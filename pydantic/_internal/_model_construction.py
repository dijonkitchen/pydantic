"""
Private logic for creating models.
"""
from __future__ import annotations as _annotations

import re
import sys
import typing
import warnings
from copy import copy
from functools import partial
from types import FunctionType
from typing import Any, Callable

from pydantic_core import SchemaSerializer, SchemaValidator, core_schema

from ..errors import PydanticUndefinedAnnotation, PydanticUserError
from ..fields import FieldInfo, ModelPrivateAttr, PrivateAttr
from . import _typing_extra
from ._core_metadata import build_metadata_dict
from ._core_utils import consolidate_refs
from ._decorators import SerializationFunctions, ValidationFunctions
from ._fields import DeferredField, Undefined
from ._generate_schema import generate_config, model_fields_schema
from ._generics import replace_types
from ._self_type import get_self_type, is_self_type
from ._utils import ClassAttribute, is_valid_identifier

if typing.TYPE_CHECKING:
    from inspect import Signature

    from ..config import ConfigDict
    from ..main import BaseModel

__all__ = 'object_setattr', 'init_private_attributes', 'inspect_namespace', 'complete_model_class'


IGNORED_TYPES: tuple[Any, ...] = (FunctionType, property, type, classmethod, staticmethod)
object_setattr = object.__setattr__


def init_private_attributes(self_: Any, **_kwargs: Any) -> None:
    """
    This method is bound to model classes to initialise private attributes.
    """
    for name, private_attr in self_.__private_attributes__.items():
        default = private_attr.get_default()
        if default is not Undefined:
            object_setattr(self_, name, default)


def inspect_namespace(namespace: dict[str, Any]) -> dict[str, ModelPrivateAttr]:
    """
    iterate over the namespace and:
    * gather private attributes
    * check for items which look like fields but are not (e.g. have no annotation) and warn
    """
    private_attributes: dict[str, ModelPrivateAttr] = {}
    raw_annotations = namespace.get('__annotations__', {})
    for var_name, value in list(namespace.items()):
        if isinstance(value, ModelPrivateAttr):
            if var_name.startswith('__'):
                raise NameError(
                    f'Private attributes "{var_name}" must not have dunder names; '
                    'Use a single underscore prefix instead.'
                )
            elif not single_underscore(var_name):
                raise NameError(
                    f'Private attributes "{var_name}" must not be a valid field name; '
                    f'Use sunder names, e. g. "_{var_name}"'
                )
            private_attributes[var_name] = value
            del namespace[var_name]
        elif not single_underscore(var_name):
            continue
        elif var_name.startswith('_'):
            private_attributes[var_name] = PrivateAttr(default=value)
            del namespace[var_name]
        elif var_name not in raw_annotations and not isinstance(value, IGNORED_TYPES):
            warnings.warn(
                f'All fields must include a type annotation; '
                f'{var_name!r} looks like a field but has no type annotation.',
                DeprecationWarning,
            )

    for ann_name in raw_annotations:
        if single_underscore(ann_name) and ann_name not in private_attributes:
            private_attributes[ann_name] = PrivateAttr()

    return private_attributes


def single_underscore(name: str) -> bool:
    return name.startswith('_') and not name.startswith('__')


def deferred_model_get_pydantic_validation_schema(
    cls: type[BaseModel], types_namespace: dict[str, Any] | None, typevars_map: dict[Any, Any] | None, **_kwargs: Any
) -> core_schema.CoreSchema:
    """
    Used on model as `__get_pydantic_core_schema__` if not all type hints are available.

    This method generates the schema for the model and also sets `model_fields`, but it does NOT build
    the validator and set `__pydantic_validator__` as that would fail in some cases - e.g. mutually referencing
    models.
    """
    inner_schema, fields = build_inner_schema(
        cls,
        cls.__name__,
        cls.__pydantic_validator_functions__,
        cls.__pydantic_serializer_functions__,
        cls.__bases__,
        types_namespace,
        typevars_map,
    )

    core_config = generate_config(cls)
    # we have to set model_fields as otherwise `repr` on the model will fail
    cls.model_fields = fields
    model_post_init = '__pydantic_post_init__' if hasattr(cls, '__pydantic_post_init__') else None
    js_metadata = cls.model_json_schema_metadata()
    return core_schema.model_schema(
        cls,
        inner_schema,
        config=core_config,
        call_after_init=model_post_init,
        metadata=build_metadata_dict(js_metadata=js_metadata),
    )


def complete_model_class(
    cls: type[BaseModel],
    name: str,
    validator_functions: ValidationFunctions,
    serialization_functions: SerializationFunctions,
    bases: tuple[type[Any], ...],
    *,
    raise_errors: bool = True,
    types_namespace: dict[str, Any] | None = None,
    typevars_map: dict[str, Any] | None = None,
) -> bool:
    """
    Collect bound validator functions, build the model validation schema and set the model signature.

    Returns `True` if the model is successfully completed, else `False`.

    This logic must be called after class has been created since validation functions must be bound
    and `get_type_hints` requires a class object.
    """
    validator_functions.set_bound_functions(cls)
    serialization_functions.set_bound_functions(cls)

    try:
        inner_schema, fields = build_inner_schema(
            cls, name, validator_functions, serialization_functions, bases, types_namespace, typevars_map
        )
    except PydanticUndefinedAnnotation as e:
        if raise_errors:
            raise
        if cls.model_config['undefined_types_warning']:
            config_warning_string = (
                f'`{name}` has an undefined annotation: `{e}`. '
                f'It may be possible to resolve this by setting '
                f'undefined_types_warning=False in the config for `{name}`.'
            )
            raise UserWarning(config_warning_string)
        usage_warning_string = (
            f'`{name}` is not fully defined; you should define `{e}`, then call `{name}.model_rebuild()` '
            f'before the first `{name}` instance is created.'
        )
        cls.__pydantic_validator__ = MockValidator(usage_warning_string)  # type: ignore[assignment]
        # here we have to set __get_pydantic_core_schema__ so we can try to rebuild the model later
        cls.__get_pydantic_core_schema__ = partial(  # type: ignore[attr-defined]
            deferred_model_get_pydantic_validation_schema, cls, typevars_map=typevars_map
        )
        return False
    inner_schema = consolidate_refs(inner_schema)

    validator_functions.check_for_unused()
    serialization_functions.check_for_unused()

    core_config = generate_config(cls)
    cls.model_fields = fields
    cls.__pydantic_validator__ = SchemaValidator(inner_schema, core_config)
    model_post_init = '__pydantic_post_init__' if hasattr(cls, '__pydantic_post_init__') else None
    js_metadata = cls.model_json_schema_metadata()
    cls.__pydantic_core_schema__ = outer_schema = core_schema.model_schema(
        cls,
        inner_schema,
        config=core_config,
        call_after_init=model_post_init,
        metadata=build_metadata_dict(js_metadata=js_metadata),
    )
    cls.__pydantic_serializer__ = SchemaSerializer(outer_schema, core_config)
    cls.__pydantic_model_complete__ = True

    # set __signature__ attr only for model class, but not for its instances
    cls.__signature__ = ClassAttribute(
        '__signature__', generate_model_signature(cls.__init__, fields, cls.model_config)
    )
    return True


def build_inner_schema(  # noqa: C901
    cls: type[BaseModel],
    name: str,
    validator_functions: ValidationFunctions,
    serialization_functions: SerializationFunctions,
    bases: tuple[type[Any], ...],
    types_namespace: dict[str, Any] | None = None,
    typevars_map: dict[Any, Any] = None,
) -> tuple[core_schema.CoreSchema, dict[str, FieldInfo]]:
    module_name = getattr(cls, '__module__', None)
    global_ns: dict[str, Any] | None = None
    if module_name:
        try:
            module = sys.modules[module_name]
        except KeyError:
            # happens occasionally, see https://github.com/pydantic/pydantic/issues/2363
            pass
        else:
            if types_namespace:
                global_ns = {**module.__dict__, **types_namespace}
            else:
                global_ns = module.__dict__

    model_ref = cls._model_ref()
    model_js_metadata = cls.model_json_schema_metadata()
    self_schema = core_schema.model_schema(
        cls,
        core_schema.definition_reference_schema(model_ref),
        metadata=build_metadata_dict(js_metadata=model_js_metadata),
    )
    local_ns = {name: get_self_type(self_schema, cls)}

    # get type hints and raise a PydanticUndefinedAnnotation if any types are undefined
    try:
        type_hints = _typing_extra.get_type_hints(cls, global_ns, local_ns, include_extras=True)
    except NameError as e:
        try:
            name = e.name
        except AttributeError:
            m = re.search(r".*'(.+?)'", str(e))
            if m:
                name = m.group(1)
            else:
                # should never happen
                raise
        raise PydanticUndefinedAnnotation(name) from e

    # https://docs.python.org/3/howto/annotations.html#accessing-the-annotations-dict-of-an-object-in-python-3-9-and-older
    # annotations is only used for finding fields in parent classes
    annotations = cls.__dict__.get('__annotations__', {})
    fields: dict[str, FieldInfo] = {}
    for ann_name, ann_type in type_hints.items():
        if ann_name.startswith('_') or _typing_extra.is_classvar(ann_type):
            continue

        # raise a PydanticUndefinedAnnotation if type is undefined
        if isinstance(ann_type, typing.ForwardRef):
            raise PydanticUndefinedAnnotation(ann_type.__forward_arg__)

        for base in bases:
            if hasattr(base, ann_name):
                raise NameError(
                    f'Field name "{ann_name}" shadows an attribute in parent "{base.__qualname__}"; '
                    f'you might want to use a different field name with "alias=\'{ann_name}\'".'
                )

        try:
            default = getattr(cls, ann_name)
        except AttributeError:
            # if field has no default value and is not in __annotations__ this means that it is
            # defined in a base class and we can take it from there
            if ann_name in annotations or is_self_type(ann_type):
                fields[ann_name] = FieldInfo.from_annotation(ann_type)
            else:
                model_fields_lookup: dict[str, FieldInfo] = {}
                for x in cls.__bases__[::-1]:
                    model_fields_lookup.update(getattr(x, 'model_fields', {}))
                if ann_name in model_fields_lookup:
                    # The field was present on one of the (possibly multiple) base classes
                    # copy the field to make sure typevar substitutions don't cause issues with the base classes
                    field = copy(model_fields_lookup[ann_name])
                else:
                    # The field was not found on any base clasess; this seems to be caused by fields not getting
                    # generated thanks to "short-circuiting" during the initialization of recursive models
                    # We deal with this by putting in a placeholder that will be removed from the final recursive
                    # thanks to other aspects of the class creation process.
                    field = FieldInfo.from_annotation(DeferredField())
                fields[ann_name] = field
        else:
            fields[ann_name] = FieldInfo.from_annotated_attribute(ann_type, default)
            # attributes which are fields are removed from the class namespace:
            # 1. To match the behaviour of annotation-only fields
            # 2. To avoid false positives in the NameError check above
            delattr(cls, ann_name)

    typevars_map = cls.__pydantic_generic_typevars_map__ or typevars_map
    if typevars_map:
        for field in fields.values():
            field.annotation = replace_types(field.annotation, typevars_map)
    schema = model_fields_schema(
        model_ref,
        fields,
        validator_functions,
        serialization_functions,
        cls.model_config['arbitrary_types_allowed'],
        local_ns,
        typevars_map,
    )
    return schema, fields


def generate_model_signature(init: Callable[..., None], fields: dict[str, FieldInfo], config: ConfigDict) -> Signature:
    """
    Generate signature for model based on its fields
    """
    from inspect import Parameter, Signature, signature
    from itertools import islice

    from ..config import Extra

    present_params = signature(init).parameters.values()
    merged_params: dict[str, Parameter] = {}
    var_kw = None
    use_var_kw = False

    for param in islice(present_params, 1, None):  # skip self arg
        # inspect does "clever" things to show annotations as strings because we have
        # `from __future__ import annotations` in main, we don't want that
        if param.annotation == 'Any':
            param = param.replace(annotation=Any)
        if param.kind is param.VAR_KEYWORD:
            var_kw = param
            continue
        merged_params[param.name] = param

    if var_kw:  # if custom init has no var_kw, fields which are not declared in it cannot be passed through
        allow_names = config['populate_by_name']
        for field_name, field in fields.items():
            param_name = field.alias or field_name
            if field_name in merged_params or param_name in merged_params:
                continue
            elif not is_valid_identifier(param_name):
                if allow_names and is_valid_identifier(field_name):
                    param_name = field_name
                else:
                    use_var_kw = True
                    continue

            # TODO: replace annotation with actual expected types once #1055 solved
            kwargs = {} if field.is_required() else {'default': field.get_default()}
            merged_params[param_name] = Parameter(
                param_name, Parameter.KEYWORD_ONLY, annotation=field.rebuild_annotation(), **kwargs
            )

    if config['extra'] is Extra.allow:
        use_var_kw = True

    if var_kw and use_var_kw:
        # Make sure the parameter for extra kwargs
        # does not have the same name as a field
        default_model_signature = [
            ('__pydantic_self__', Parameter.POSITIONAL_OR_KEYWORD),
            ('data', Parameter.VAR_KEYWORD),
        ]
        if [(p.name, p.kind) for p in present_params] == default_model_signature:
            # if this is the standard model signature, use extra_data as the extra args name
            var_kw_name = 'extra_data'
        else:
            # else start from var_kw
            var_kw_name = var_kw.name

        # generate a name that's definitely unique
        while var_kw_name in fields:
            var_kw_name += '_'
        merged_params[var_kw_name] = var_kw.replace(name=var_kw_name)

    return Signature(parameters=list(merged_params.values()), return_annotation=None)


class MockValidator:
    """
    Mocker for `pydantic_core.SchemaValidator` which just raises an error when one of its methods is accessed.
    """

    __slots__ = ('_error_message',)

    def __init__(self, error_message: str) -> None:
        self._error_message = error_message

    def __getattr__(self, item: str) -> None:
        __tracebackhide__ = True
        # raise an AttributeError if `item` doesn't exist
        getattr(SchemaValidator, item)
        raise PydanticUserError(self._error_message)
