# orm/strategies.py
# Copyright (C) 2005-2023 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
# mypy: ignore-errors


"""sqlalchemy.orm.interfaces.LoaderStrategy
   implementations, and related MapperOptions."""

from __future__ import annotations

import collections
import itertools
from typing import Any
from typing import Dict
from typing import Tuple
from typing import TYPE_CHECKING

from . import attributes
from . import exc as orm_exc
from . import interfaces
from . import loading
from . import path_registry
from . import properties
from . import query
from . import relationships
from . import unitofwork
from . import util as orm_util
from .base import _DEFER_FOR_STATE
from .base import _RAISE_FOR_STATE
from .base import _SET_DEFERRED_EXPIRED
from .base import ATTR_WAS_SET
from .base import LoaderCallableStatus
from .base import PASSIVE_OFF
from .base import PassiveFlag
from .context import _column_descriptions
from .context import ORMCompileState
from .context import ORMSelectCompileState
from .context import QueryContext
from .interfaces import LoaderStrategy
from .interfaces import StrategizedProperty
from .session import _state_session
from .state import InstanceState
from .strategy_options import Load
from .util import _none_set
from .util import AliasedClass
from .. import event
from .. import exc as sa_exc
from .. import inspect
from .. import log
from .. import sql
from .. import util
from ..sql import util as sql_util
from ..sql import visitors
from ..sql.selectable import LABEL_STYLE_TABLENAME_PLUS_COL
from ..sql.selectable import Select

if TYPE_CHECKING:
    from .relationships import RelationshipProperty
    from ..sql.elements import ColumnElement


def _register_attribute(
    prop,
    mapper,
    useobject,
    compare_function=None,
    typecallable=None,
    callable_=None,
    proxy_property=None,
    active_history=False,
    impl_class=None,
    **kw,
):

    listen_hooks = []

    uselist = useobject and prop.uselist

    if useobject and prop.single_parent:
        listen_hooks.append(single_parent_validator)

    if prop.key in prop.parent.validators:
        fn, opts = prop.parent.validators[prop.key]
        listen_hooks.append(
            lambda desc, prop: orm_util._validator_events(
                desc, prop.key, fn, **opts
            )
        )

    if useobject:
        listen_hooks.append(unitofwork.track_cascade_events)

    # need to assemble backref listeners
    # after the singleparentvalidator, mapper validator
    if useobject:
        backref = prop.back_populates
        if backref and prop._effective_sync_backref:
            listen_hooks.append(
                lambda desc, prop: attributes.backref_listeners(
                    desc, backref, uselist
                )
            )

    # a single MapperProperty is shared down a class inheritance
    # hierarchy, so we set up attribute instrumentation and backref event
    # for each mapper down the hierarchy.

    # typically, "mapper" is the same as prop.parent, due to the way
    # the configure_mappers() process runs, however this is not strongly
    # enforced, and in the case of a second configure_mappers() run the
    # mapper here might not be prop.parent; also, a subclass mapper may
    # be called here before a superclass mapper.  That is, can't depend
    # on mappers not already being set up so we have to check each one.

    for m in mapper.self_and_descendants:
        if prop is m._props.get(
            prop.key
        ) and not m.class_manager._attr_has_impl(prop.key):

            desc = attributes.register_attribute_impl(
                m.class_,
                prop.key,
                parent_token=prop,
                uselist=uselist,
                compare_function=compare_function,
                useobject=useobject,
                trackparent=useobject
                and (
                    prop.single_parent
                    or prop.direction is interfaces.ONETOMANY
                ),
                typecallable=typecallable,
                callable_=callable_,
                active_history=active_history,
                impl_class=impl_class,
                send_modified_events=not useobject or not prop.viewonly,
                doc=prop.doc,
                **kw,
            )

            for hook in listen_hooks:
                hook(desc, prop)


@properties.ColumnProperty.strategy_for(instrument=False, deferred=False)
class UninstrumentedColumnLoader(LoaderStrategy):
    """Represent a non-instrumented MapperProperty.

    The polymorphic_on argument of mapper() often results in this,
    if the argument is against the with_polymorphic selectable.

    """

    __slots__ = ("columns",)

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        self.columns = self.parent_property.columns

    def setup_query(
        self,
        compile_state,
        query_entity,
        path,
        loadopt,
        adapter,
        column_collection=None,
        **kwargs,
    ):
        for c in self.columns:
            if adapter:
                c = adapter.columns[c]
            compile_state._append_dedupe_col_collection(c, column_collection)

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        pass


@log.class_logger
@properties.ColumnProperty.strategy_for(instrument=True, deferred=False)
class ColumnLoader(LoaderStrategy):
    """Provide loading behavior for a :class:`.ColumnProperty`."""

    __slots__ = "columns", "is_composite"

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        self.columns = self.parent_property.columns
        self.is_composite = hasattr(self.parent_property, "composite_class")

    def setup_query(
        self,
        compile_state,
        query_entity,
        path,
        loadopt,
        adapter,
        column_collection,
        memoized_populators,
        check_for_adapt=False,
        **kwargs,
    ):
        for c in self.columns:
            if adapter:
                if check_for_adapt:
                    c = adapter.adapt_check_present(c)
                    if c is None:
                        return
                else:
                    c = adapter.columns[c]

            compile_state._append_dedupe_col_collection(c, column_collection)

        fetch = self.columns[0]
        if adapter:
            fetch = adapter.columns[fetch]
            if fetch is None:
                # None happens here only for dml bulk_persistence cases
                # when context.DMLReturningColFilter is used
                return

        memoized_populators[self.parent_property] = fetch

    def init_class_attribute(self, mapper):
        self.is_class_level = True
        coltype = self.columns[0].type
        # TODO: check all columns ?  check for foreign key as well?
        active_history = (
            self.parent_property.active_history
            or self.columns[0].primary_key
            or (
                mapper.version_id_col is not None
                and mapper._columntoproperty.get(mapper.version_id_col, None)
                is self.parent_property
            )
        )

        _register_attribute(
            self.parent_property,
            mapper,
            useobject=False,
            compare_function=coltype.compare_values,
            active_history=active_history,
        )

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        # look through list of columns represented here
        # to see which, if any, is present in the row.

        for col in self.columns:
            if adapter:
                col = adapter.columns[col]
            getter = result._getter(col, False)
            if getter:
                populators["quick"].append((self.key, getter))
                break
        else:
            populators["expire"].append((self.key, True))


@log.class_logger
@properties.ColumnProperty.strategy_for(query_expression=True)
class ExpressionColumnLoader(ColumnLoader):
    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)

        # compare to the "default" expression that is mapped in
        # the column.   If it's sql.null, we don't need to render
        # unless an expr is passed in the options.
        null = sql.null().label(None)
        self._have_default_expression = any(
            not c.compare(null) for c in self.parent_property.columns
        )

    def setup_query(
        self,
        compile_state,
        query_entity,
        path,
        loadopt,
        adapter,
        column_collection,
        memoized_populators,
        **kwargs,
    ):
        columns = None
        if loadopt and "expression" in loadopt.local_opts:
            columns = [loadopt.local_opts["expression"]]
        elif self._have_default_expression:
            columns = self.parent_property.columns

        if columns is None:
            return

        for c in columns:
            if adapter:
                c = adapter.columns[c]
            compile_state._append_dedupe_col_collection(c, column_collection)

        fetch = columns[0]
        if adapter:
            fetch = adapter.columns[fetch]
            if fetch is None:
                # None is not expected to be the result of any
                # adapter implementation here, however there may be theoretical
                # usages of returning() with context.DMLReturningColFilter
                return

        memoized_populators[self.parent_property] = fetch

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        # look through list of columns represented here
        # to see which, if any, is present in the row.
        if loadopt and "expression" in loadopt.local_opts:
            columns = [loadopt.local_opts["expression"]]

            for col in columns:
                if adapter:
                    col = adapter.columns[col]
                getter = result._getter(col, False)
                if getter:
                    populators["quick"].append((self.key, getter))
                    break
            else:
                populators["expire"].append((self.key, True))

    def init_class_attribute(self, mapper):
        self.is_class_level = True

        _register_attribute(
            self.parent_property,
            mapper,
            useobject=False,
            compare_function=self.columns[0].type.compare_values,
            accepts_scalar_loader=False,
        )


@log.class_logger
@properties.ColumnProperty.strategy_for(deferred=True, instrument=True)
@properties.ColumnProperty.strategy_for(
    deferred=True, instrument=True, raiseload=True
)
@properties.ColumnProperty.strategy_for(do_nothing=True)
class DeferredColumnLoader(LoaderStrategy):
    """Provide loading behavior for a deferred :class:`.ColumnProperty`."""

    __slots__ = "columns", "group", "raiseload"

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        if hasattr(self.parent_property, "composite_class"):
            raise NotImplementedError(
                "Deferred loading for composite " "types not implemented yet"
            )
        self.raiseload = self.strategy_opts.get("raiseload", False)
        self.columns = self.parent_property.columns
        self.group = self.parent_property.group

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):

        # for a DeferredColumnLoader, this method is only used during a
        # "row processor only" query; see test_deferred.py ->
        # tests with "rowproc_only" in their name.  As of the 1.0 series,
        # loading._instance_processor doesn't use a "row processing" function
        # to populate columns, instead it uses data in the "populators"
        # dictionary.  Normally, the DeferredColumnLoader.setup_query()
        # sets up that data in the "memoized_populators" dictionary
        # and "create_row_processor()" here is never invoked.

        if (
            context.refresh_state
            and context.query._compile_options._only_load_props
            and self.key in context.query._compile_options._only_load_props
        ):
            self.parent_property._get_strategy(
                (("deferred", False), ("instrument", True))
            ).create_row_processor(
                context,
                query_entity,
                path,
                loadopt,
                mapper,
                result,
                adapter,
                populators,
            )

        elif not self.is_class_level:
            if self.raiseload:
                set_deferred_for_local_state = (
                    self.parent_property._raise_column_loader
                )
            else:
                set_deferred_for_local_state = (
                    self.parent_property._deferred_column_loader
                )
            populators["new"].append((self.key, set_deferred_for_local_state))
        else:
            populators["expire"].append((self.key, False))

    def init_class_attribute(self, mapper):
        self.is_class_level = True

        _register_attribute(
            self.parent_property,
            mapper,
            useobject=False,
            compare_function=self.columns[0].type.compare_values,
            callable_=self._load_for_state,
            load_on_unexpire=False,
        )

    def setup_query(
        self,
        compile_state,
        query_entity,
        path,
        loadopt,
        adapter,
        column_collection,
        memoized_populators,
        only_load_props=None,
        **kw,
    ):

        if (
            (
                compile_state.compile_options._render_for_subquery
                and self.parent_property._renders_in_subqueries
            )
            or (
                loadopt
                and set(self.columns).intersection(
                    self.parent._should_undefer_in_wildcard
                )
            )
            or (
                loadopt
                and self.group
                and loadopt.local_opts.get(
                    "undefer_group_%s" % self.group, False
                )
            )
            or (only_load_props and self.key in only_load_props)
        ):
            self.parent_property._get_strategy(
                (("deferred", False), ("instrument", True))
            ).setup_query(
                compile_state,
                query_entity,
                path,
                loadopt,
                adapter,
                column_collection,
                memoized_populators,
                **kw,
            )
        elif self.is_class_level:
            memoized_populators[self.parent_property] = _SET_DEFERRED_EXPIRED
        elif not self.raiseload:
            memoized_populators[self.parent_property] = _DEFER_FOR_STATE
        else:
            memoized_populators[self.parent_property] = _RAISE_FOR_STATE

    def _load_for_state(self, state, passive):
        if not state.key:
            return LoaderCallableStatus.ATTR_EMPTY

        if not passive & PassiveFlag.SQL_OK:
            return LoaderCallableStatus.PASSIVE_NO_RESULT

        localparent = state.manager.mapper

        if self.group:
            toload = [
                p.key
                for p in localparent.iterate_properties
                if isinstance(p, StrategizedProperty)
                and isinstance(p.strategy, DeferredColumnLoader)
                and p.group == self.group
            ]
        else:
            toload = [self.key]

        # narrow the keys down to just those which have no history
        group = [k for k in toload if k in state.unmodified]

        session = _state_session(state)
        if session is None:
            raise orm_exc.DetachedInstanceError(
                "Parent instance %s is not bound to a Session; "
                "deferred load operation of attribute '%s' cannot proceed"
                % (orm_util.state_str(state), self.key)
            )

        if self.raiseload:
            self._invoke_raise_load(state, passive, "raise")

        loading.load_scalar_attributes(
            state.mapper, state, set(group), PASSIVE_OFF
        )

        return LoaderCallableStatus.ATTR_WAS_SET

    def _invoke_raise_load(self, state, passive, lazy):
        raise sa_exc.InvalidRequestError(
            "'%s' is not available due to raiseload=True" % (self,)
        )


class LoadDeferredColumns:
    """serializable loader object used by DeferredColumnLoader"""

    def __init__(self, key: str, raiseload: bool = False):
        self.key = key
        self.raiseload = raiseload

    def __call__(self, state, passive=attributes.PASSIVE_OFF):
        key = self.key

        localparent = state.manager.mapper
        prop = localparent._props[key]
        if self.raiseload:
            strategy_key = (
                ("deferred", True),
                ("instrument", True),
                ("raiseload", True),
            )
        else:
            strategy_key = (("deferred", True), ("instrument", True))
        strategy = prop._get_strategy(strategy_key)
        return strategy._load_for_state(state, passive)


class AbstractRelationshipLoader(LoaderStrategy):
    """LoaderStratgies which deal with related objects."""

    __slots__ = "mapper", "target", "uselist", "entity"

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        self.mapper = self.parent_property.mapper
        self.entity = self.parent_property.entity
        self.target = self.parent_property.target
        self.uselist = self.parent_property.uselist


@log.class_logger
@relationships.RelationshipProperty.strategy_for(do_nothing=True)
class DoNothingLoader(LoaderStrategy):
    """Relationship loader that makes no change to the object's state.

    Compared to NoLoader, this loader does not initialize the
    collection/attribute to empty/none; the usual default LazyLoader will
    take effect.

    """


@log.class_logger
@relationships.RelationshipProperty.strategy_for(lazy="noload")
@relationships.RelationshipProperty.strategy_for(lazy=None)
class NoLoader(AbstractRelationshipLoader):
    """Provide loading behavior for a :class:`.Relationship`
    with "lazy=None".

    """

    __slots__ = ()

    def init_class_attribute(self, mapper):
        self.is_class_level = True

        _register_attribute(
            self.parent_property,
            mapper,
            useobject=True,
            typecallable=self.parent_property.collection_class,
        )

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        def invoke_no_load(state, dict_, row):
            if self.uselist:
                attributes.init_state_collection(state, dict_, self.key)
            else:
                dict_[self.key] = None

        populators["new"].append((self.key, invoke_no_load))


@log.class_logger
@relationships.RelationshipProperty.strategy_for(lazy=True)
@relationships.RelationshipProperty.strategy_for(lazy="select")
@relationships.RelationshipProperty.strategy_for(lazy="raise")
@relationships.RelationshipProperty.strategy_for(lazy="raise_on_sql")
@relationships.RelationshipProperty.strategy_for(lazy="baked_select")
class LazyLoader(
    AbstractRelationshipLoader, util.MemoizedSlots, log.Identified
):
    """Provide loading behavior for a :class:`.Relationship`
    with "lazy=True", that is loads when first accessed.

    """

    __slots__ = (
        "_lazywhere",
        "_rev_lazywhere",
        "_lazyload_reverse_option",
        "_order_by",
        "use_get",
        "is_aliased_class",
        "_bind_to_col",
        "_equated_columns",
        "_rev_bind_to_col",
        "_rev_equated_columns",
        "_simple_lazy_clause",
        "_raise_always",
        "_raise_on_sql",
    )

    _lazywhere: ColumnElement[bool]
    _bind_to_col: Dict[str, ColumnElement[Any]]
    _rev_lazywhere: ColumnElement[bool]
    _rev_bind_to_col: Dict[str, ColumnElement[Any]]

    parent_property: RelationshipProperty[Any]

    def __init__(
        self, parent: RelationshipProperty[Any], strategy_key: Tuple[Any, ...]
    ):
        super().__init__(parent, strategy_key)
        self._raise_always = self.strategy_opts["lazy"] == "raise"
        self._raise_on_sql = self.strategy_opts["lazy"] == "raise_on_sql"

        self.is_aliased_class = inspect(self.entity).is_aliased_class

        join_condition = self.parent_property._join_condition
        (
            self._lazywhere,
            self._bind_to_col,
            self._equated_columns,
        ) = join_condition.create_lazy_clause()

        (
            self._rev_lazywhere,
            self._rev_bind_to_col,
            self._rev_equated_columns,
        ) = join_condition.create_lazy_clause(reverse_direction=True)

        if self.parent_property.order_by:
            self._order_by = [
                sql_util._deep_annotate(elem, {"_orm_adapt": True})
                for elem in util.to_list(self.parent_property.order_by)
            ]
        else:
            self._order_by = None

        self.logger.info("%s lazy loading clause %s", self, self._lazywhere)

        # determine if our "lazywhere" clause is the same as the mapper's
        # get() clause.  then we can just use mapper.get()
        #
        # TODO: the "not self.uselist" can be taken out entirely; a m2o
        # load that populates for a list (very unusual, but is possible with
        # the API) can still set for "None" and the attribute system will
        # populate as an empty list.
        self.use_get = (
            not self.is_aliased_class
            and not self.uselist
            and self.entity._get_clause[0].compare(
                self._lazywhere,
                use_proxies=True,
                compare_keys=False,
                equivalents=self.mapper._equivalent_columns,
            )
        )

        if self.use_get:
            for col in list(self._equated_columns):
                if col in self.mapper._equivalent_columns:
                    for c in self.mapper._equivalent_columns[col]:
                        self._equated_columns[c] = self._equated_columns[col]

            self.logger.info(
                "%s will use Session.get() to " "optimize instance loads", self
            )

    def init_class_attribute(self, mapper):
        self.is_class_level = True

        _legacy_inactive_history_style = (
            self.parent_property._legacy_inactive_history_style
        )

        if self.parent_property.active_history:
            active_history = True
            _deferred_history = False

        elif (
            self.parent_property.direction is not interfaces.MANYTOONE
            or not self.use_get
        ):
            if _legacy_inactive_history_style:
                active_history = True
                _deferred_history = False
            else:
                active_history = False
                _deferred_history = True
        else:
            active_history = _deferred_history = False

        _register_attribute(
            self.parent_property,
            mapper,
            useobject=True,
            callable_=self._load_for_state,
            typecallable=self.parent_property.collection_class,
            active_history=active_history,
            _deferred_history=_deferred_history,
        )

    def _memoized_attr__simple_lazy_clause(self):

        lazywhere = sql_util._deep_annotate(
            self._lazywhere, {"_orm_adapt": True}
        )

        criterion, bind_to_col = (lazywhere, self._bind_to_col)

        params = []

        def visit_bindparam(bindparam):
            bindparam.unique = False

        visitors.traverse(criterion, {}, {"bindparam": visit_bindparam})

        def visit_bindparam(bindparam):
            if bindparam._identifying_key in bind_to_col:
                params.append(
                    (
                        bindparam.key,
                        bind_to_col[bindparam._identifying_key],
                        None,
                    )
                )
            elif bindparam.callable is None:
                params.append((bindparam.key, None, bindparam.value))

        criterion = visitors.cloned_traverse(
            criterion, {}, {"bindparam": visit_bindparam}
        )

        return criterion, params

    def _generate_lazy_clause(self, state, passive):
        criterion, param_keys = self._simple_lazy_clause

        if state is None:
            return sql_util.adapt_criterion_to_null(
                criterion, [key for key, ident, value in param_keys]
            )

        mapper = self.parent_property.parent

        o = state.obj()  # strong ref
        dict_ = attributes.instance_dict(o)

        if passive & PassiveFlag.INIT_OK:
            passive ^= PassiveFlag.INIT_OK

        params = {}
        for key, ident, value in param_keys:
            if ident is not None:
                if passive and passive & PassiveFlag.LOAD_AGAINST_COMMITTED:
                    value = mapper._get_committed_state_attr_by_column(
                        state, dict_, ident, passive
                    )
                else:
                    value = mapper._get_state_attr_by_column(
                        state, dict_, ident, passive
                    )

            params[key] = value

        return criterion, params

    def _invoke_raise_load(self, state, passive, lazy):
        raise sa_exc.InvalidRequestError(
            "'%s' is not available due to lazy='%s'" % (self, lazy)
        )

    def _load_for_state(
        self,
        state,
        passive,
        loadopt=None,
        extra_criteria=(),
        extra_options=(),
        alternate_effective_path=None,
        execution_options=util.EMPTY_DICT,
    ):
        if not state.key and (
            (
                not self.parent_property.load_on_pending
                and not state._load_pending
            )
            or not state.session_id
        ):
            return LoaderCallableStatus.ATTR_EMPTY

        pending = not state.key
        primary_key_identity = None

        use_get = self.use_get and (not loadopt or not loadopt._extra_criteria)

        if (not passive & PassiveFlag.SQL_OK and not use_get) or (
            not passive & attributes.NON_PERSISTENT_OK and pending
        ):
            return LoaderCallableStatus.PASSIVE_NO_RESULT

        if (
            # we were given lazy="raise"
            self._raise_always
            # the no_raise history-related flag was not passed
            and not passive & PassiveFlag.NO_RAISE
            and (
                # if we are use_get and related_object_ok is disabled,
                # which means we are at most looking in the identity map
                # for history purposes or otherwise returning
                # PASSIVE_NO_RESULT, don't raise.  This is also a
                # history-related flag
                not use_get
                or passive & PassiveFlag.RELATED_OBJECT_OK
            )
        ):

            self._invoke_raise_load(state, passive, "raise")

        session = _state_session(state)
        if not session:
            if passive & PassiveFlag.NO_RAISE:
                return LoaderCallableStatus.PASSIVE_NO_RESULT

            raise orm_exc.DetachedInstanceError(
                "Parent instance %s is not bound to a Session; "
                "lazy load operation of attribute '%s' cannot proceed"
                % (orm_util.state_str(state), self.key)
            )

        # if we have a simple primary key load, check the
        # identity map without generating a Query at all
        if use_get:
            primary_key_identity = self._get_ident_for_use_get(
                session, state, passive
            )
            if LoaderCallableStatus.PASSIVE_NO_RESULT in primary_key_identity:
                return LoaderCallableStatus.PASSIVE_NO_RESULT
            elif LoaderCallableStatus.NEVER_SET in primary_key_identity:
                return LoaderCallableStatus.NEVER_SET

            if _none_set.issuperset(primary_key_identity):
                return None

            if (
                self.key in state.dict
                and not passive & PassiveFlag.DEFERRED_HISTORY_LOAD
            ):
                return LoaderCallableStatus.ATTR_WAS_SET

            # look for this identity in the identity map.  Delegate to the
            # Query class in use, as it may have special rules for how it
            # does this, including how it decides what the correct
            # identity_token would be for this identity.

            instance = session._identity_lookup(
                self.entity,
                primary_key_identity,
                passive=passive,
                lazy_loaded_from=state,
            )

            if instance is not None:
                if instance is LoaderCallableStatus.PASSIVE_CLASS_MISMATCH:
                    return None
                else:
                    return instance
            elif (
                not passive & PassiveFlag.SQL_OK
                or not passive & PassiveFlag.RELATED_OBJECT_OK
            ):
                return LoaderCallableStatus.PASSIVE_NO_RESULT

        return self._emit_lazyload(
            session,
            state,
            primary_key_identity,
            passive,
            loadopt,
            extra_criteria,
            extra_options,
            alternate_effective_path,
            execution_options,
        )

    def _get_ident_for_use_get(self, session, state, passive):
        instance_mapper = state.manager.mapper

        if passive & PassiveFlag.LOAD_AGAINST_COMMITTED:
            get_attr = instance_mapper._get_committed_state_attr_by_column
        else:
            get_attr = instance_mapper._get_state_attr_by_column

        dict_ = state.dict

        return [
            get_attr(state, dict_, self._equated_columns[pk], passive=passive)
            for pk in self.mapper.primary_key
        ]

    @util.preload_module("sqlalchemy.orm.strategy_options")
    def _emit_lazyload(
        self,
        session,
        state,
        primary_key_identity,
        passive,
        loadopt,
        extra_criteria,
        extra_options,
        alternate_effective_path,
        execution_options,
    ):
        strategy_options = util.preloaded.orm_strategy_options

        clauseelement = self.entity.__clause_element__()
        stmt = Select._create_raw_select(
            _raw_columns=[clauseelement],
            _propagate_attrs=clauseelement._propagate_attrs,
            _label_style=LABEL_STYLE_TABLENAME_PLUS_COL,
            _compile_options=ORMCompileState.default_compile_options,
        )
        load_options = QueryContext.default_load_options

        load_options += {
            "_invoke_all_eagers": False,
            "_lazy_loaded_from": state,
        }

        if self.parent_property.secondary is not None:
            stmt = stmt.select_from(
                self.mapper, self.parent_property.secondary
            )

        pending = not state.key

        # don't autoflush on pending
        if pending or passive & attributes.NO_AUTOFLUSH:
            stmt._execution_options = util.immutabledict({"autoflush": False})

        use_get = self.use_get

        if state.load_options or (loadopt and loadopt._extra_criteria):
            if alternate_effective_path is None:
                effective_path = state.load_path[self.parent_property]
            else:
                effective_path = alternate_effective_path[self.parent_property]

            opts = state.load_options

            if loadopt and loadopt._extra_criteria:
                use_get = False
                opts += (
                    orm_util.LoaderCriteriaOption(self.entity, extra_criteria),
                )

            stmt._with_options = opts
        elif alternate_effective_path is None:
            # this path is used if there are not already any options
            # in the query, but an event may want to add them
            effective_path = state.mapper._path_registry[self.parent_property]
        else:
            # added by immediateloader
            effective_path = alternate_effective_path[self.parent_property]

        if extra_options:
            stmt._with_options += extra_options
        stmt._compile_options += {"_current_path": effective_path}

        if use_get:
            if self._raise_on_sql and not passive & PassiveFlag.NO_RAISE:
                self._invoke_raise_load(state, passive, "raise_on_sql")

            return loading.load_on_pk_identity(
                session,
                stmt,
                primary_key_identity,
                load_options=load_options,
                execution_options=execution_options,
            )

        if self._order_by:
            stmt._order_by_clauses = self._order_by

        def _lazyload_reverse(compile_context):
            for rev in self.parent_property._reverse_property:
                # reverse props that are MANYTOONE are loading *this*
                # object from get(), so don't need to eager out to those.
                if (
                    rev.direction is interfaces.MANYTOONE
                    and rev._use_get
                    and not isinstance(rev.strategy, LazyLoader)
                ):
                    strategy_options.Load._construct_for_existing_path(
                        compile_context.compile_options._current_path[
                            rev.parent
                        ]
                    ).lazyload(rev).process_compile_state(compile_context)

        stmt._with_context_options += (
            (_lazyload_reverse, self.parent_property),
        )

        lazy_clause, params = self._generate_lazy_clause(state, passive)

        if execution_options:

            execution_options = util.EMPTY_DICT.merge_with(
                execution_options,
                {
                    "_sa_orm_load_options": load_options,
                },
            )
        else:
            execution_options = {
                "_sa_orm_load_options": load_options,
            }

        if (
            self.key in state.dict
            and not passive & PassiveFlag.DEFERRED_HISTORY_LOAD
        ):
            return LoaderCallableStatus.ATTR_WAS_SET

        if pending:
            if util.has_intersection(orm_util._none_set, params.values()):
                return None

        elif util.has_intersection(orm_util._never_set, params.values()):
            return None

        if self._raise_on_sql and not passive & PassiveFlag.NO_RAISE:
            self._invoke_raise_load(state, passive, "raise_on_sql")

        stmt._where_criteria = (lazy_clause,)

        result = session.execute(
            stmt, params, execution_options=execution_options
        )

        result = result.unique().scalars().all()

        if self.uselist:
            return result
        else:
            l = len(result)
            if l:
                if l > 1:
                    util.warn(
                        "Multiple rows returned with "
                        "uselist=False for lazily-loaded attribute '%s' "
                        % self.parent_property
                    )

                return result[0]
            else:
                return None

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        key = self.key

        if not self.is_class_level or (loadopt and loadopt._extra_criteria):
            # we are not the primary manager for this attribute
            # on this class - set up a
            # per-instance lazyloader, which will override the
            # class-level behavior.
            # this currently only happens when using a
            # "lazyload" option on a "no load"
            # attribute - "eager" attributes always have a
            # class-level lazyloader installed.
            set_lazy_callable = (
                InstanceState._instance_level_callable_processor
            )(
                mapper.class_manager,
                LoadLazyAttribute(
                    key,
                    self,
                    loadopt,
                    loadopt._generate_extra_criteria(context)
                    if loadopt._extra_criteria
                    else None,
                ),
                key,
            )

            populators["new"].append((self.key, set_lazy_callable))
        elif context.populate_existing or mapper.always_refresh:

            def reset_for_lazy_callable(state, dict_, row):
                # we are the primary manager for this attribute on
                # this class - reset its
                # per-instance attribute state, so that the class-level
                # lazy loader is
                # executed when next referenced on this instance.
                # this is needed in
                # populate_existing() types of scenarios to reset
                # any existing state.
                state._reset(dict_, key)

            populators["new"].append((self.key, reset_for_lazy_callable))


class LoadLazyAttribute:
    """semi-serializable loader object used by LazyLoader

    Historically, this object would be carried along with instances that
    needed to run lazyloaders, so it had to be serializable to support
    cached instances.

    this is no longer a general requirement, and the case where this object
    is used is exactly the case where we can't really serialize easily,
    which is when extra criteria in the loader option is present.

    We can't reliably serialize that as it refers to mapped entities and
    AliasedClass objects that are local to the current process, which would
    need to be matched up on deserialize e.g. the sqlalchemy.ext.serializer
    approach.

    """

    def __init__(self, key, initiating_strategy, loadopt, extra_criteria):
        self.key = key
        self.strategy_key = initiating_strategy.strategy_key
        self.loadopt = loadopt
        self.extra_criteria = extra_criteria

    def __getstate__(self):
        if self.extra_criteria is not None:
            util.warn(
                "Can't reliably serialize a lazyload() option that "
                "contains additional criteria; please use eager loading "
                "for this case"
            )
        return {
            "key": self.key,
            "strategy_key": self.strategy_key,
            "loadopt": self.loadopt,
            "extra_criteria": (),
        }

    def __call__(self, state, passive=attributes.PASSIVE_OFF):
        key = self.key
        instance_mapper = state.manager.mapper
        prop = instance_mapper._props[key]
        strategy = prop._strategies[self.strategy_key]

        return strategy._load_for_state(
            state,
            passive,
            loadopt=self.loadopt,
            extra_criteria=self.extra_criteria,
        )


class PostLoader(AbstractRelationshipLoader):
    """A relationship loader that emits a second SELECT statement."""

    __slots__ = ()

    def _setup_for_recursion(self, context, path, loadopt, join_depth=None):

        effective_path = (
            context.compile_state.current_path or orm_util.PathRegistry.root
        ) + path

        top_level_context = context._get_top_level_context()
        execution_options = util.immutabledict(
            {"sa_top_level_orm_context": top_level_context}
        )

        if loadopt:
            recursion_depth = loadopt.local_opts.get("recursion_depth", None)
            unlimited_recursion = recursion_depth == -1
        else:
            recursion_depth = None
            unlimited_recursion = False

        if recursion_depth is not None:
            if not self.parent_property._is_self_referential:
                raise sa_exc.InvalidRequestError(
                    f"recursion_depth option on relationship "
                    f"{self.parent_property} not valid for "
                    "non-self-referential relationship"
                )
            recursion_depth = context.execution_options.get(
                f"_recursion_depth_{id(self)}", recursion_depth
            )

            if not unlimited_recursion and recursion_depth < 0:
                return (
                    effective_path,
                    False,
                    execution_options,
                    recursion_depth,
                )

            if not unlimited_recursion:
                execution_options = execution_options.union(
                    {
                        f"_recursion_depth_{id(self)}": recursion_depth - 1,
                    }
                )

        if loading.PostLoad.path_exists(
            context, effective_path, self.parent_property
        ):
            return effective_path, False, execution_options, recursion_depth

        path_w_prop = path[self.parent_property]
        effective_path_w_prop = effective_path[self.parent_property]

        if not path_w_prop.contains(context.attributes, "loader"):
            if join_depth:
                if effective_path_w_prop.length / 2 > join_depth:
                    return (
                        effective_path,
                        False,
                        execution_options,
                        recursion_depth,
                    )
            elif effective_path_w_prop.contains_mapper(self.mapper):
                return (
                    effective_path,
                    False,
                    execution_options,
                    recursion_depth,
                )

        return effective_path, True, execution_options, recursion_depth

    def _immediateload_create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        return self.parent_property._get_strategy(
            (("lazy", "immediate"),)
        ).create_row_processor(
            context,
            query_entity,
            path,
            loadopt,
            mapper,
            result,
            adapter,
            populators,
        )


@relationships.RelationshipProperty.strategy_for(lazy="immediate")
class ImmediateLoader(PostLoader):
    __slots__ = ()

    def init_class_attribute(self, mapper):
        self.parent_property._get_strategy(
            (("lazy", "select"),)
        ).init_class_attribute(mapper)

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):

        (
            effective_path,
            run_loader,
            execution_options,
            recursion_depth,
        ) = self._setup_for_recursion(context, path, loadopt)
        if not run_loader:
            # this will not emit SQL and will only emit for a many-to-one
            # "use get" load.   the "_RELATED" part means it may return
            # instance even if its expired, since this is a mutually-recursive
            # load operation.
            flags = attributes.PASSIVE_NO_FETCH_RELATED | PassiveFlag.NO_RAISE
        else:
            flags = attributes.PASSIVE_OFF | PassiveFlag.NO_RAISE

        loading.PostLoad.callable_for_path(
            context,
            effective_path,
            self.parent,
            self.parent_property,
            self._load_for_path,
            loadopt,
            flags,
            recursion_depth,
            execution_options,
        )

    def _load_for_path(
        self,
        context,
        path,
        states,
        load_only,
        loadopt,
        flags,
        recursion_depth,
        execution_options,
    ):

        if recursion_depth:
            new_opt = Load(loadopt.path.entity)
            new_opt.context = (
                loadopt,
                loadopt._recurse(),
            )
            alternate_effective_path = path._truncate_recursive()
            extra_options = (new_opt,)
        else:
            new_opt = None
            alternate_effective_path = path
            extra_options = ()

        key = self.key
        lazyloader = self.parent_property._get_strategy((("lazy", "select"),))
        for state, overwrite in states:
            dict_ = state.dict

            if overwrite or key not in dict_:
                value = lazyloader._load_for_state(
                    state,
                    flags,
                    extra_options=extra_options,
                    alternate_effective_path=alternate_effective_path,
                    execution_options=execution_options,
                )
                if value is not ATTR_WAS_SET:
                    state.get_impl(key).set_committed_value(
                        state, dict_, value
                    )


@log.class_logger
@relationships.RelationshipProperty.strategy_for(lazy="subquery")
class SubqueryLoader(PostLoader):
    __slots__ = ("join_depth",)

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        self.join_depth = self.parent_property.join_depth

    def init_class_attribute(self, mapper):
        self.parent_property._get_strategy(
            (("lazy", "select"),)
        ).init_class_attribute(mapper)

    def _get_leftmost(
        self,
        orig_query_entity_index,
        subq_path,
        current_compile_state,
        is_root,
    ):
        given_subq_path = subq_path
        subq_path = subq_path.path
        subq_mapper = orm_util._class_to_mapper(subq_path[0])

        # determine attributes of the leftmost mapper
        if (
            self.parent.isa(subq_mapper)
            and self.parent_property is subq_path[1]
        ):
            leftmost_mapper, leftmost_prop = self.parent, self.parent_property
        else:
            leftmost_mapper, leftmost_prop = subq_mapper, subq_path[1]

        if is_root:
            # the subq_path is also coming from cached state, so when we start
            # building up this path, it has to also be converted to be in terms
            # of the current state. this is for the specific case of the entity
            # is an AliasedClass against a subquery that's not otherwise going
            # to adapt
            new_subq_path = current_compile_state._entities[
                orig_query_entity_index
            ].entity_zero._path_registry[leftmost_prop]
            additional = len(subq_path) - len(new_subq_path)
            if additional:
                new_subq_path += path_registry.PathRegistry.coerce(
                    subq_path[-additional:]
                )
        else:
            new_subq_path = given_subq_path

        leftmost_cols = leftmost_prop.local_columns

        leftmost_attr = [
            getattr(
                new_subq_path.path[0].entity,
                leftmost_mapper._columntoproperty[c].key,
            )
            for c in leftmost_cols
        ]

        return leftmost_mapper, leftmost_attr, leftmost_prop, new_subq_path

    def _generate_from_original_query(
        self,
        orig_compile_state,
        orig_query,
        leftmost_mapper,
        leftmost_attr,
        leftmost_relationship,
        orig_entity,
    ):
        # reformat the original query
        # to look only for significant columns
        q = orig_query._clone().correlate(None)

        # LEGACY: make a Query back from the select() !!
        # This suits at least two legacy cases:
        # 1. applications which expect before_compile() to be called
        #    below when we run .subquery() on this query (Keystone)
        # 2. applications which are doing subqueryload with complex
        #    from_self() queries, as query.subquery() / .statement
        #    has to do the full compile context for multiply-nested
        #    from_self() (Neutron) - see test_subqload_from_self
        #    for demo.
        q2 = query.Query.__new__(query.Query)
        q2.__dict__.update(q.__dict__)
        q = q2

        # set the query's "FROM" list explicitly to what the
        # FROM list would be in any case, as we will be limiting
        # the columns in the SELECT list which may no longer include
        # all entities mentioned in things like WHERE, JOIN, etc.
        if not q._from_obj:
            q._enable_assertions = False
            q.select_from.non_generative(
                q,
                *{
                    ent["entity"]
                    for ent in _column_descriptions(
                        orig_query, compile_state=orig_compile_state
                    )
                    if ent["entity"] is not None
                },
            )

        # select from the identity columns of the outer (specifically, these
        # are the 'local_cols' of the property).  This will remove other
        # columns from the query that might suggest the right entity which is
        # why we do set select_from above.   The attributes we have are
        # coerced and adapted using the original query's adapter, which is
        # needed only for the case of adapting a subclass column to
        # that of a polymorphic selectable, e.g. we have
        # Engineer.primary_language and the entity is Person.  All other
        # adaptations, e.g. from_self, select_entity_from(), will occur
        # within the new query when it compiles, as the compile_state we are
        # using here is only a partial one.  If the subqueryload is from a
        # with_polymorphic() or other aliased() object, left_attr will already
        # be the correct attributes so no adaptation is needed.
        target_cols = orig_compile_state._adapt_col_list(
            [
                sql.coercions.expect(sql.roles.ColumnsClauseRole, o)
                for o in leftmost_attr
            ],
            orig_compile_state._get_current_adapter(),
        )
        q._raw_columns = target_cols

        distinct_target_key = leftmost_relationship.distinct_target_key

        if distinct_target_key is True:
            q._distinct = True
        elif distinct_target_key is None:
            # if target_cols refer to a non-primary key or only
            # part of a composite primary key, set the q as distinct
            for t in {c.table for c in target_cols}:
                if not set(target_cols).issuperset(t.primary_key):
                    q._distinct = True
                    break

        # don't need ORDER BY if no limit/offset
        if not q._has_row_limiting_clause:
            q._order_by_clauses = ()

        if q._distinct is True and q._order_by_clauses:
            # the logic to automatically add the order by columns to the query
            # when distinct is True is deprecated in the query
            to_add = sql_util.expand_column_list_from_order_by(
                target_cols, q._order_by_clauses
            )
            if to_add:
                q._set_entities(target_cols + to_add)

        # the original query now becomes a subquery
        # which we'll join onto.
        # LEGACY: as "q" is a Query, the before_compile() event is invoked
        # here.
        embed_q = q.set_label_style(LABEL_STYLE_TABLENAME_PLUS_COL).subquery()
        left_alias = orm_util.AliasedClass(
            leftmost_mapper, embed_q, use_mapper_path=True
        )
        return left_alias

    def _prep_for_joins(self, left_alias, subq_path):
        # figure out what's being joined.  a.k.a. the fun part
        to_join = []
        pairs = list(subq_path.pairs())

        for i, (mapper, prop) in enumerate(pairs):
            if i > 0:
                # look at the previous mapper in the chain -
                # if it is as or more specific than this prop's
                # mapper, use that instead.
                # note we have an assumption here that
                # the non-first element is always going to be a mapper,
                # not an AliasedClass

                prev_mapper = pairs[i - 1][1].mapper
                to_append = prev_mapper if prev_mapper.isa(mapper) else mapper
            else:
                to_append = mapper

            to_join.append((to_append, prop.key))

        # determine the immediate parent class we are joining from,
        # which needs to be aliased.

        if len(to_join) < 2:
            # in the case of a one level eager load, this is the
            # leftmost "left_alias".
            parent_alias = left_alias
        else:
            info = inspect(to_join[-1][0])
            if info.is_aliased_class:
                parent_alias = info.entity
            else:
                # alias a plain mapper as we may be
                # joining multiple times
                parent_alias = orm_util.AliasedClass(
                    info.entity, use_mapper_path=True
                )

        local_cols = self.parent_property.local_columns

        local_attr = [
            getattr(parent_alias, self.parent._columntoproperty[c].key)
            for c in local_cols
        ]
        return to_join, local_attr, parent_alias

    def _apply_joins(
        self, q, to_join, left_alias, parent_alias, effective_entity
    ):

        ltj = len(to_join)
        if ltj == 1:
            to_join = [
                getattr(left_alias, to_join[0][1]).of_type(effective_entity)
            ]
        elif ltj == 2:
            to_join = [
                getattr(left_alias, to_join[0][1]).of_type(parent_alias),
                getattr(parent_alias, to_join[-1][1]).of_type(
                    effective_entity
                ),
            ]
        elif ltj > 2:
            middle = [
                (
                    orm_util.AliasedClass(item[0])
                    if not inspect(item[0]).is_aliased_class
                    else item[0].entity,
                    item[1],
                )
                for item in to_join[1:-1]
            ]
            inner = []

            while middle:
                item = middle.pop(0)
                attr = getattr(item[0], item[1])
                if middle:
                    attr = attr.of_type(middle[0][0])
                else:
                    attr = attr.of_type(parent_alias)

                inner.append(attr)

            to_join = (
                [getattr(left_alias, to_join[0][1]).of_type(inner[0].parent)]
                + inner
                + [
                    getattr(parent_alias, to_join[-1][1]).of_type(
                        effective_entity
                    )
                ]
            )

        for attr in to_join:
            q = q.join(attr)

        return q

    def _setup_options(
        self,
        context,
        q,
        subq_path,
        rewritten_path,
        orig_query,
        effective_entity,
        loadopt,
    ):

        # note that because the subqueryload object
        # does not re-use the cached query, instead always making
        # use of the current invoked query, while we have two queries
        # here (orig and context.query), they are both non-cached
        # queries and we can transfer the options as is without
        # adjusting for new criteria.   Some work on #6881 / #6889
        # brought this into question.
        new_options = orig_query._with_options

        if loadopt and loadopt._extra_criteria:

            new_options += (
                orm_util.LoaderCriteriaOption(
                    self.entity,
                    loadopt._generate_extra_criteria(context),
                ),
            )

        # propagate loader options etc. to the new query.
        # these will fire relative to subq_path.
        q = q._with_current_path(rewritten_path)
        q = q.options(*new_options)

        return q

    def _setup_outermost_orderby(self, q):
        if self.parent_property.order_by:

            def _setup_outermost_orderby(compile_context):
                compile_context.eager_order_by += tuple(
                    util.to_list(self.parent_property.order_by)
                )

            q = q._add_context_option(
                _setup_outermost_orderby, self.parent_property
            )

        return q

    class _SubqCollections:
        """Given a :class:`_query.Query` used to emit the "subquery load",
        provide a load interface that executes the query at the
        first moment a value is needed.

        """

        __slots__ = (
            "session",
            "execution_options",
            "load_options",
            "params",
            "subq",
            "_data",
        )

        def __init__(self, context, subq):
            # avoid creating a cycle by storing context
            # even though that's preferable
            self.session = context.session
            self.execution_options = context.execution_options
            self.load_options = context.load_options
            self.params = context.params or {}
            self.subq = subq
            self._data = None

        def get(self, key, default):
            if self._data is None:
                self._load()
            return self._data.get(key, default)

        def _load(self):
            self._data = collections.defaultdict(list)

            q = self.subq
            assert q.session is None

            q = q.with_session(self.session)

            if self.load_options._populate_existing:
                q = q.populate_existing()
            # to work with baked query, the parameters may have been
            # updated since this query was created, so take these into account

            rows = list(q.params(self.params))
            for k, v in itertools.groupby(rows, lambda x: x[1:]):
                self._data[k].extend(vv[0] for vv in v)

        def loader(self, state, dict_, row):
            if self._data is None:
                self._load()

    def _setup_query_from_rowproc(
        self,
        context,
        query_entity,
        path,
        entity,
        loadopt,
        adapter,
    ):
        compile_state = context.compile_state
        if (
            not compile_state.compile_options._enable_eagerloads
            or compile_state.compile_options._for_refresh_state
        ):
            return

        orig_query_entity_index = compile_state._entities.index(query_entity)
        context.loaders_require_buffering = True

        path = path[self.parent_property]

        # build up a path indicating the path from the leftmost
        # entity to the thing we're subquery loading.
        with_poly_entity = path.get(
            compile_state.attributes, "path_with_polymorphic", None
        )
        if with_poly_entity is not None:
            effective_entity = with_poly_entity
        else:
            effective_entity = self.entity

        subq_path, rewritten_path = context.query._execution_options.get(
            ("subquery_paths", None),
            (orm_util.PathRegistry.root, orm_util.PathRegistry.root),
        )
        is_root = subq_path is orm_util.PathRegistry.root
        subq_path = subq_path + path
        rewritten_path = rewritten_path + path

        # use the current query being invoked, not the compile state
        # one.  this is so that we get the current parameters.  however,
        # it means we can't use the existing compile state, we have to make
        # a new one.    other approaches include possibly using the
        # compiled query but swapping the params, seems only marginally
        # less time spent but more complicated
        orig_query = context.query._execution_options.get(
            ("orig_query", SubqueryLoader), context.query
        )

        # make a new compile_state for the query that's probably cached, but
        # we're sort of undoing a bit of that caching :(
        compile_state_cls = ORMCompileState._get_plugin_class_for_plugin(
            orig_query, "orm"
        )

        if orig_query._is_lambda_element:
            if context.load_options._lazy_loaded_from is None:
                util.warn(
                    'subqueryloader for "%s" must invoke lambda callable '
                    "at %r in "
                    "order to produce a new query, decreasing the efficiency "
                    "of caching for this statement.  Consider using "
                    "selectinload() for more effective full-lambda caching"
                    % (self, orig_query)
                )
            orig_query = orig_query._resolved

        # this is the more "quick" version, however it's not clear how
        # much of this we need.    in particular I can't get a test to
        # fail if the "set_base_alias" is missing and not sure why that is.
        orig_compile_state = compile_state_cls._create_entities_collection(
            orig_query, legacy=False
        )

        (
            leftmost_mapper,
            leftmost_attr,
            leftmost_relationship,
            rewritten_path,
        ) = self._get_leftmost(
            orig_query_entity_index,
            rewritten_path,
            orig_compile_state,
            is_root,
        )

        # generate a new Query from the original, then
        # produce a subquery from it.
        left_alias = self._generate_from_original_query(
            orig_compile_state,
            orig_query,
            leftmost_mapper,
            leftmost_attr,
            leftmost_relationship,
            entity,
        )

        # generate another Query that will join the
        # left alias to the target relationships.
        # basically doing a longhand
        # "from_self()".  (from_self() itself not quite industrial
        # strength enough for all contingencies...but very close)

        q = query.Query(effective_entity)

        q._execution_options = q._execution_options.union(
            {
                ("orig_query", SubqueryLoader): orig_query,
                ("subquery_paths", None): (subq_path, rewritten_path),
            }
        )

        q = q._set_enable_single_crit(False)
        to_join, local_attr, parent_alias = self._prep_for_joins(
            left_alias, subq_path
        )

        q = q.add_columns(*local_attr)
        q = self._apply_joins(
            q, to_join, left_alias, parent_alias, effective_entity
        )

        q = self._setup_options(
            context,
            q,
            subq_path,
            rewritten_path,
            orig_query,
            effective_entity,
            loadopt,
        )
        q = self._setup_outermost_orderby(q)

        return q

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):

        if context.refresh_state:
            return self._immediateload_create_row_processor(
                context,
                query_entity,
                path,
                loadopt,
                mapper,
                result,
                adapter,
                populators,
            )

        _, run_loader, _, _ = self._setup_for_recursion(
            context, path, loadopt, self.join_depth
        )
        if not run_loader:
            return

        if not isinstance(context.compile_state, ORMSelectCompileState):
            # issue 7505 - subqueryload() in 1.3 and previous would silently
            # degrade for from_statement() without warning. this behavior
            # is restored here
            return

        if not self.parent.class_manager[self.key].impl.supports_population:
            raise sa_exc.InvalidRequestError(
                "'%s' does not support object "
                "population - eager loading cannot be applied." % self
            )

        # a little dance here as the "path" is still something that only
        # semi-tracks the exact series of things we are loading, still not
        # telling us about with_polymorphic() and stuff like that when it's at
        # the root..  the initial MapperEntity is more accurate for this case.
        if len(path) == 1:
            if not orm_util._entity_isa(query_entity.entity_zero, self.parent):
                return
        elif not orm_util._entity_isa(path[-1], self.parent):
            return

        subq = self._setup_query_from_rowproc(
            context,
            query_entity,
            path,
            path[-1],
            loadopt,
            adapter,
        )

        if subq is None:
            return

        assert subq.session is None

        path = path[self.parent_property]

        local_cols = self.parent_property.local_columns

        # cache the loaded collections in the context
        # so that inheriting mappers don't re-load when they
        # call upon create_row_processor again
        collections = path.get(context.attributes, "collections")
        if collections is None:
            collections = self._SubqCollections(context, subq)
            path.set(context.attributes, "collections", collections)

        if adapter:
            local_cols = [adapter.columns[c] for c in local_cols]

        if self.uselist:
            self._create_collection_loader(
                context, result, collections, local_cols, populators
            )
        else:
            self._create_scalar_loader(
                context, result, collections, local_cols, populators
            )

    def _create_collection_loader(
        self, context, result, collections, local_cols, populators
    ):
        tuple_getter = result._tuple_getter(local_cols)

        def load_collection_from_subq(state, dict_, row):
            collection = collections.get(tuple_getter(row), ())
            state.get_impl(self.key).set_committed_value(
                state, dict_, collection
            )

        def load_collection_from_subq_existing_row(state, dict_, row):
            if self.key not in dict_:
                load_collection_from_subq(state, dict_, row)

        populators["new"].append((self.key, load_collection_from_subq))
        populators["existing"].append(
            (self.key, load_collection_from_subq_existing_row)
        )

        if context.invoke_all_eagers:
            populators["eager"].append((self.key, collections.loader))

    def _create_scalar_loader(
        self, context, result, collections, local_cols, populators
    ):
        tuple_getter = result._tuple_getter(local_cols)

        def load_scalar_from_subq(state, dict_, row):
            collection = collections.get(tuple_getter(row), (None,))
            if len(collection) > 1:
                util.warn(
                    "Multiple rows returned with "
                    "uselist=False for eagerly-loaded attribute '%s' " % self
                )

            scalar = collection[0]
            state.get_impl(self.key).set_committed_value(state, dict_, scalar)

        def load_scalar_from_subq_existing_row(state, dict_, row):
            if self.key not in dict_:
                load_scalar_from_subq(state, dict_, row)

        populators["new"].append((self.key, load_scalar_from_subq))
        populators["existing"].append(
            (self.key, load_scalar_from_subq_existing_row)
        )
        if context.invoke_all_eagers:
            populators["eager"].append((self.key, collections.loader))


@log.class_logger
@relationships.RelationshipProperty.strategy_for(lazy="joined")
@relationships.RelationshipProperty.strategy_for(lazy=False)
class JoinedLoader(AbstractRelationshipLoader):
    """Provide loading behavior for a :class:`.Relationship`
    using joined eager loading.

    """

    __slots__ = "join_depth", "_aliased_class_pool"

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        self.join_depth = self.parent_property.join_depth
        self._aliased_class_pool = []

    def init_class_attribute(self, mapper):
        self.parent_property._get_strategy(
            (("lazy", "select"),)
        ).init_class_attribute(mapper)

    def setup_query(
        self,
        compile_state,
        query_entity,
        path,
        loadopt,
        adapter,
        column_collection=None,
        parentmapper=None,
        chained_from_outerjoin=False,
        **kwargs,
    ):
        """Add a left outer join to the statement that's being constructed."""

        if not compile_state.compile_options._enable_eagerloads:
            return
        elif self.uselist:
            compile_state.multi_row_eager_loaders = True

        path = path[self.parent_property]

        with_polymorphic = None

        user_defined_adapter = (
            self._init_user_defined_eager_proc(
                loadopt, compile_state, compile_state.attributes
            )
            if loadopt
            else False
        )

        if user_defined_adapter is not False:

            # setup an adapter but dont create any JOIN, assume it's already
            # in the query
            (
                clauses,
                adapter,
                add_to_collection,
            ) = self._setup_query_on_user_defined_adapter(
                compile_state,
                query_entity,
                path,
                adapter,
                user_defined_adapter,
            )

            # don't do "wrap" for multi-row, we want to wrap
            # limited/distinct SELECT,
            # because we want to put the JOIN on the outside.

        else:
            # if not via query option, check for
            # a cycle
            if not path.contains(compile_state.attributes, "loader"):
                if self.join_depth:
                    if path.length / 2 > self.join_depth:
                        return
                elif path.contains_mapper(self.mapper):
                    return

            # add the JOIN and create an adapter
            (
                clauses,
                adapter,
                add_to_collection,
                chained_from_outerjoin,
            ) = self._generate_row_adapter(
                compile_state,
                query_entity,
                path,
                loadopt,
                adapter,
                column_collection,
                parentmapper,
                chained_from_outerjoin,
            )

            # for multi-row, we want to wrap limited/distinct SELECT,
            # because we want to put the JOIN on the outside.
            compile_state.eager_adding_joins = True

        with_poly_entity = path.get(
            compile_state.attributes, "path_with_polymorphic", None
        )
        if with_poly_entity is not None:
            with_polymorphic = inspect(
                with_poly_entity
            ).with_polymorphic_mappers
        else:
            with_polymorphic = None

        path = path[self.entity]

        loading._setup_entity_query(
            compile_state,
            self.mapper,
            query_entity,
            path,
            clauses,
            add_to_collection,
            with_polymorphic=with_polymorphic,
            parentmapper=self.mapper,
            chained_from_outerjoin=chained_from_outerjoin,
        )

        if with_poly_entity is not None and None in set(
            compile_state.secondary_columns
        ):
            raise sa_exc.InvalidRequestError(
                "Detected unaliased columns when generating joined "
                "load.  Make sure to use aliased=True or flat=True "
                "when using joined loading with with_polymorphic()."
            )

    def _init_user_defined_eager_proc(
        self, loadopt, compile_state, target_attributes
    ):

        # check if the opt applies at all
        if "eager_from_alias" not in loadopt.local_opts:
            # nope
            return False

        path = loadopt.path.parent

        # the option applies.  check if the "user_defined_eager_row_processor"
        # has been built up.
        adapter = path.get(
            compile_state.attributes, "user_defined_eager_row_processor", False
        )
        if adapter is not False:
            # just return it
            return adapter

        # otherwise figure it out.
        alias = loadopt.local_opts["eager_from_alias"]
        root_mapper, prop = path[-2:]

        if alias is not None:
            if isinstance(alias, str):
                alias = prop.target.alias(alias)
            adapter = orm_util.ORMAdapter(
                orm_util._TraceAdaptRole.JOINEDLOAD_USER_DEFINED_ALIAS,
                prop.mapper,
                selectable=alias,
                equivalents=prop.mapper._equivalent_columns,
                limit_on_entity=False,
            )
        else:
            if path.contains(
                compile_state.attributes, "path_with_polymorphic"
            ):
                with_poly_entity = path.get(
                    compile_state.attributes, "path_with_polymorphic"
                )
                adapter = orm_util.ORMAdapter(
                    orm_util._TraceAdaptRole.JOINEDLOAD_PATH_WITH_POLYMORPHIC,
                    with_poly_entity,
                    equivalents=prop.mapper._equivalent_columns,
                )
            else:
                adapter = compile_state._polymorphic_adapters.get(
                    prop.mapper, None
                )
        path.set(
            target_attributes,
            "user_defined_eager_row_processor",
            adapter,
        )

        return adapter

    def _setup_query_on_user_defined_adapter(
        self, context, entity, path, adapter, user_defined_adapter
    ):

        # apply some more wrapping to the "user defined adapter"
        # if we are setting up the query for SQL render.
        adapter = entity._get_entity_clauses(context)

        if adapter and user_defined_adapter:
            user_defined_adapter = user_defined_adapter.wrap(adapter)
            path.set(
                context.attributes,
                "user_defined_eager_row_processor",
                user_defined_adapter,
            )
        elif adapter:
            user_defined_adapter = adapter
            path.set(
                context.attributes,
                "user_defined_eager_row_processor",
                user_defined_adapter,
            )

        add_to_collection = context.primary_columns
        return user_defined_adapter, adapter, add_to_collection

    def _gen_pooled_aliased_class(self, context):
        # keep a local pool of AliasedClass objects that get re-used.
        # we need one unique AliasedClass per query per appearance of our
        # entity in the query.

        if inspect(self.entity).is_aliased_class:
            alt_selectable = inspect(self.entity).selectable
        else:
            alt_selectable = None

        key = ("joinedloader_ac", self)
        if key not in context.attributes:
            context.attributes[key] = idx = 0
        else:
            context.attributes[key] = idx = context.attributes[key] + 1

        if idx >= len(self._aliased_class_pool):
            to_adapt = orm_util.AliasedClass(
                self.mapper,
                alias=alt_selectable._anonymous_fromclause(flat=True)
                if alt_selectable is not None
                else None,
                flat=True,
                use_mapper_path=True,
            )

            # load up the .columns collection on the Alias() before
            # the object becomes shared among threads.  this prevents
            # races for column identities.
            inspect(to_adapt).selectable.c
            self._aliased_class_pool.append(to_adapt)

        return self._aliased_class_pool[idx]

    def _generate_row_adapter(
        self,
        compile_state,
        entity,
        path,
        loadopt,
        adapter,
        column_collection,
        parentmapper,
        chained_from_outerjoin,
    ):
        with_poly_entity = path.get(
            compile_state.attributes, "path_with_polymorphic", None
        )
        if with_poly_entity:
            to_adapt = with_poly_entity
        else:
            to_adapt = self._gen_pooled_aliased_class(compile_state)

        to_adapt_insp = inspect(to_adapt)

        clauses = to_adapt_insp._memo(
            ("joinedloader_ormadapter", self),
            orm_util.ORMAdapter,
            orm_util._TraceAdaptRole.JOINEDLOAD_MEMOIZED_ADAPTER,
            to_adapt_insp,
            equivalents=self.mapper._equivalent_columns,
            adapt_required=True,
            allow_label_resolve=False,
            anonymize_labels=True,
        )

        assert clauses.is_aliased_class

        innerjoin = (
            loadopt.local_opts.get("innerjoin", self.parent_property.innerjoin)
            if loadopt is not None
            else self.parent_property.innerjoin
        )

        if not innerjoin:
            # if this is an outer join, all non-nested eager joins from
            # this path must also be outer joins
            chained_from_outerjoin = True

        compile_state.create_eager_joins.append(
            (
                self._create_eager_join,
                entity,
                path,
                adapter,
                parentmapper,
                clauses,
                innerjoin,
                chained_from_outerjoin,
                loadopt._extra_criteria if loadopt else (),
            )
        )

        add_to_collection = compile_state.secondary_columns
        path.set(compile_state.attributes, "eager_row_processor", clauses)

        return clauses, adapter, add_to_collection, chained_from_outerjoin

    def _create_eager_join(
        self,
        compile_state,
        query_entity,
        path,
        adapter,
        parentmapper,
        clauses,
        innerjoin,
        chained_from_outerjoin,
        extra_criteria,
    ):
        if parentmapper is None:
            localparent = query_entity.mapper
        else:
            localparent = parentmapper

        # whether or not the Query will wrap the selectable in a subquery,
        # and then attach eager load joins to that (i.e., in the case of
        # LIMIT/OFFSET etc.)
        should_nest_selectable = (
            compile_state.multi_row_eager_loaders
            and compile_state._should_nest_selectable
        )

        query_entity_key = None

        if (
            query_entity not in compile_state.eager_joins
            and not should_nest_selectable
            and compile_state.from_clauses
        ):

            indexes = sql_util.find_left_clause_that_matches_given(
                compile_state.from_clauses, query_entity.selectable
            )

            if len(indexes) > 1:
                # for the eager load case, I can't reproduce this right
                # now.   For query.join() I can.
                raise sa_exc.InvalidRequestError(
                    "Can't identify which query entity in which to joined "
                    "eager load from.   Please use an exact match when "
                    "specifying the join path."
                )

            if indexes:
                clause = compile_state.from_clauses[indexes[0]]
                # join to an existing FROM clause on the query.
                # key it to its list index in the eager_joins dict.
                # Query._compile_context will adapt as needed and
                # append to the FROM clause of the select().
                query_entity_key, default_towrap = indexes[0], clause

        if query_entity_key is None:
            query_entity_key, default_towrap = (
                query_entity,
                query_entity.selectable,
            )

        towrap = compile_state.eager_joins.setdefault(
            query_entity_key, default_towrap
        )

        if adapter:
            if getattr(adapter, "is_aliased_class", False):
                # joining from an adapted entity.  The adapted entity
                # might be a "with_polymorphic", so resolve that to our
                # specific mapper's entity before looking for our attribute
                # name on it.
                efm = adapter.aliased_insp._entity_for_mapper(
                    localparent
                    if localparent.isa(self.parent)
                    else self.parent
                )

                # look for our attribute on the adapted entity, else fall back
                # to our straight property
                onclause = getattr(efm.entity, self.key, self.parent_property)
            else:
                onclause = getattr(
                    orm_util.AliasedClass(
                        self.parent, adapter.selectable, use_mapper_path=True
                    ),
                    self.key,
                    self.parent_property,
                )

        else:
            onclause = self.parent_property

        assert clauses.is_aliased_class

        attach_on_outside = (
            not chained_from_outerjoin
            or not innerjoin
            or innerjoin == "unnested"
            or query_entity.entity_zero.represents_outer_join
        )

        extra_join_criteria = extra_criteria
        additional_entity_criteria = compile_state.global_attributes.get(
            ("additional_entity_criteria", self.mapper), ()
        )
        if additional_entity_criteria:
            extra_join_criteria += tuple(
                ae._resolve_where_criteria(self.mapper)
                for ae in additional_entity_criteria
                if ae.propagate_to_loaders
            )

        if attach_on_outside:
            # this is the "classic" eager join case.
            eagerjoin = orm_util._ORMJoin(
                towrap,
                clauses.aliased_insp,
                onclause,
                isouter=not innerjoin
                or query_entity.entity_zero.represents_outer_join
                or (chained_from_outerjoin and isinstance(towrap, sql.Join)),
                _left_memo=self.parent,
                _right_memo=self.mapper,
                _extra_criteria=extra_join_criteria,
            )
        else:
            # all other cases are innerjoin=='nested' approach
            eagerjoin = self._splice_nested_inner_join(
                path, towrap, clauses, onclause, extra_join_criteria
            )

        compile_state.eager_joins[query_entity_key] = eagerjoin

        # send a hint to the Query as to where it may "splice" this join
        eagerjoin.stop_on = query_entity.selectable

        if not parentmapper:
            # for parentclause that is the non-eager end of the join,
            # ensure all the parent cols in the primaryjoin are actually
            # in the
            # columns clause (i.e. are not deferred), so that aliasing applied
            # by the Query propagates those columns outward.
            # This has the effect
            # of "undefering" those columns.
            for col in sql_util._find_columns(
                self.parent_property.primaryjoin
            ):
                if localparent.persist_selectable.c.contains_column(col):
                    if adapter:
                        col = adapter.columns[col]
                    compile_state._append_dedupe_col_collection(
                        col, compile_state.primary_columns
                    )

        if self.parent_property.order_by:
            compile_state.eager_order_by += tuple(
                (eagerjoin._target_adapter.copy_and_process)(
                    util.to_list(self.parent_property.order_by)
                )
            )

    def _splice_nested_inner_join(
        self, path, join_obj, clauses, onclause, extra_criteria, splicing=False
    ):

        # recursive fn to splice a nested join into an existing one.
        # splicing=False means this is the outermost call, and it
        # should return a value.  splicing=<from object> is the recursive
        # form, where it can return None to indicate the end of the recursion

        if splicing is False:
            # first call is always handed a join object
            # from the outside
            assert isinstance(join_obj, orm_util._ORMJoin)
        elif isinstance(join_obj, sql.selectable.FromGrouping):
            return self._splice_nested_inner_join(
                path,
                join_obj.element,
                clauses,
                onclause,
                extra_criteria,
                splicing,
            )
        elif not isinstance(join_obj, orm_util._ORMJoin):
            if path[-2].isa(splicing):
                return orm_util._ORMJoin(
                    join_obj,
                    clauses.aliased_insp,
                    onclause,
                    isouter=False,
                    _left_memo=splicing,
                    _right_memo=path[-1].mapper,
                    _extra_criteria=extra_criteria,
                )
            else:
                return None

        target_join = self._splice_nested_inner_join(
            path,
            join_obj.right,
            clauses,
            onclause,
            extra_criteria,
            join_obj._right_memo,
        )
        if target_join is None:
            right_splice = False
            target_join = self._splice_nested_inner_join(
                path,
                join_obj.left,
                clauses,
                onclause,
                extra_criteria,
                join_obj._left_memo,
            )
            if target_join is None:
                # should only return None when recursively called,
                # e.g. splicing refers to a from obj
                assert (
                    splicing is not False
                ), "assertion failed attempting to produce joined eager loads"
                return None
        else:
            right_splice = True

        if right_splice:
            # for a right splice, attempt to flatten out
            # a JOIN b JOIN c JOIN .. to avoid needless
            # parenthesis nesting
            if not join_obj.isouter and not target_join.isouter:
                eagerjoin = join_obj._splice_into_center(target_join)
            else:
                eagerjoin = orm_util._ORMJoin(
                    join_obj.left,
                    target_join,
                    join_obj.onclause,
                    isouter=join_obj.isouter,
                    _left_memo=join_obj._left_memo,
                )
        else:
            eagerjoin = orm_util._ORMJoin(
                target_join,
                join_obj.right,
                join_obj.onclause,
                isouter=join_obj.isouter,
                _right_memo=join_obj._right_memo,
            )

        eagerjoin._target_adapter = target_join._target_adapter
        return eagerjoin

    def _create_eager_adapter(self, context, result, adapter, path, loadopt):
        compile_state = context.compile_state

        user_defined_adapter = (
            self._init_user_defined_eager_proc(
                loadopt, compile_state, context.attributes
            )
            if loadopt
            else False
        )

        if user_defined_adapter is not False:
            decorator = user_defined_adapter
            # user defined eagerloads are part of the "primary"
            # portion of the load.
            # the adapters applied to the Query should be honored.
            if compile_state.compound_eager_adapter and decorator:
                decorator = decorator.wrap(
                    compile_state.compound_eager_adapter
                )
            elif compile_state.compound_eager_adapter:
                decorator = compile_state.compound_eager_adapter
        else:
            decorator = path.get(
                compile_state.attributes, "eager_row_processor"
            )
            if decorator is None:
                return False

        if self.mapper._result_has_identity_key(result, decorator):
            return decorator
        else:
            # no identity key - don't return a row
            # processor, will cause a degrade to lazy
            return False

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):
        if not self.parent.class_manager[self.key].impl.supports_population:
            raise sa_exc.InvalidRequestError(
                "'%s' does not support object "
                "population - eager loading cannot be applied." % self
            )

        if self.uselist:
            context.loaders_require_uniquing = True

        our_path = path[self.parent_property]

        eager_adapter = self._create_eager_adapter(
            context, result, adapter, our_path, loadopt
        )

        if eager_adapter is not False:
            key = self.key

            _instance = loading._instance_processor(
                query_entity,
                self.mapper,
                context,
                result,
                our_path[self.entity],
                eager_adapter,
            )

            if not self.uselist:
                self._create_scalar_loader(context, key, _instance, populators)
            else:
                self._create_collection_loader(
                    context, key, _instance, populators
                )
        else:
            self.parent_property._get_strategy(
                (("lazy", "select"),)
            ).create_row_processor(
                context,
                query_entity,
                path,
                loadopt,
                mapper,
                result,
                adapter,
                populators,
            )

    def _create_collection_loader(self, context, key, _instance, populators):
        def load_collection_from_joined_new_row(state, dict_, row):
            # note this must unconditionally clear out any existing collection.
            # an existing collection would be present only in the case of
            # populate_existing().
            collection = attributes.init_state_collection(state, dict_, key)
            result_list = util.UniqueAppender(
                collection, "append_without_event"
            )
            context.attributes[(state, key)] = result_list
            inst = _instance(row)
            if inst is not None:
                result_list.append(inst)

        def load_collection_from_joined_existing_row(state, dict_, row):
            if (state, key) in context.attributes:
                result_list = context.attributes[(state, key)]
            else:
                # appender_key can be absent from context.attributes
                # with isnew=False when self-referential eager loading
                # is used; the same instance may be present in two
                # distinct sets of result columns
                collection = attributes.init_state_collection(
                    state, dict_, key
                )
                result_list = util.UniqueAppender(
                    collection, "append_without_event"
                )
                context.attributes[(state, key)] = result_list
            inst = _instance(row)
            if inst is not None:
                result_list.append(inst)

        def load_collection_from_joined_exec(state, dict_, row):
            _instance(row)

        populators["new"].append(
            (self.key, load_collection_from_joined_new_row)
        )
        populators["existing"].append(
            (self.key, load_collection_from_joined_existing_row)
        )
        if context.invoke_all_eagers:
            populators["eager"].append(
                (self.key, load_collection_from_joined_exec)
            )

    def _create_scalar_loader(self, context, key, _instance, populators):
        def load_scalar_from_joined_new_row(state, dict_, row):
            # set a scalar object instance directly on the parent
            # object, bypassing InstrumentedAttribute event handlers.
            dict_[key] = _instance(row)

        def load_scalar_from_joined_existing_row(state, dict_, row):
            # call _instance on the row, even though the object has
            # been created, so that we further descend into properties
            existing = _instance(row)

            # conflicting value already loaded, this shouldn't happen
            if key in dict_:
                if existing is not dict_[key]:
                    util.warn(
                        "Multiple rows returned with "
                        "uselist=False for eagerly-loaded attribute '%s' "
                        % self
                    )
            else:
                # this case is when one row has multiple loads of the
                # same entity (e.g. via aliasing), one has an attribute
                # that the other doesn't.
                dict_[key] = existing

        def load_scalar_from_joined_exec(state, dict_, row):
            _instance(row)

        populators["new"].append((self.key, load_scalar_from_joined_new_row))
        populators["existing"].append(
            (self.key, load_scalar_from_joined_existing_row)
        )
        if context.invoke_all_eagers:
            populators["eager"].append(
                (self.key, load_scalar_from_joined_exec)
            )


@log.class_logger
@relationships.RelationshipProperty.strategy_for(lazy="selectin")
class SelectInLoader(PostLoader, util.MemoizedSlots):
    __slots__ = (
        "join_depth",
        "omit_join",
        "_parent_alias",
        "_query_info",
        "_fallback_query_info",
    )

    query_info = collections.namedtuple(
        "queryinfo",
        [
            "load_only_child",
            "load_with_join",
            "in_expr",
            "pk_cols",
            "zero_idx",
            "child_lookup_cols",
        ],
    )

    _chunksize = 500

    def __init__(self, parent, strategy_key):
        super().__init__(parent, strategy_key)
        self.join_depth = self.parent_property.join_depth
        is_m2o = self.parent_property.direction is interfaces.MANYTOONE

        if self.parent_property.omit_join is not None:
            self.omit_join = self.parent_property.omit_join
        else:
            lazyloader = self.parent_property._get_strategy(
                (("lazy", "select"),)
            )
            if is_m2o:
                self.omit_join = lazyloader.use_get
            else:
                self.omit_join = self.parent._get_clause[0].compare(
                    lazyloader._rev_lazywhere,
                    use_proxies=True,
                    compare_keys=False,
                    equivalents=self.parent._equivalent_columns,
                )

        if self.omit_join:
            if is_m2o:
                self._query_info = self._init_for_omit_join_m2o()
                self._fallback_query_info = self._init_for_join()
            else:
                self._query_info = self._init_for_omit_join()
        else:
            self._query_info = self._init_for_join()

    def _init_for_omit_join(self):
        pk_to_fk = dict(
            self.parent_property._join_condition.local_remote_pairs
        )
        pk_to_fk.update(
            (equiv, pk_to_fk[k])
            for k in list(pk_to_fk)
            for equiv in self.parent._equivalent_columns.get(k, ())
        )

        pk_cols = fk_cols = [
            pk_to_fk[col] for col in self.parent.primary_key if col in pk_to_fk
        ]
        if len(fk_cols) > 1:
            in_expr = sql.tuple_(*fk_cols)
            zero_idx = False
        else:
            in_expr = fk_cols[0]
            zero_idx = True

        return self.query_info(False, False, in_expr, pk_cols, zero_idx, None)

    def _init_for_omit_join_m2o(self):
        pk_cols = self.mapper.primary_key
        if len(pk_cols) > 1:
            in_expr = sql.tuple_(*pk_cols)
            zero_idx = False
        else:
            in_expr = pk_cols[0]
            zero_idx = True

        lazyloader = self.parent_property._get_strategy((("lazy", "select"),))
        lookup_cols = [lazyloader._equated_columns[pk] for pk in pk_cols]

        return self.query_info(
            True, False, in_expr, pk_cols, zero_idx, lookup_cols
        )

    def _init_for_join(self):
        self._parent_alias = AliasedClass(self.parent.class_)
        pa_insp = inspect(self._parent_alias)
        pk_cols = [
            pa_insp._adapt_element(col) for col in self.parent.primary_key
        ]
        if len(pk_cols) > 1:
            in_expr = sql.tuple_(*pk_cols)
            zero_idx = False
        else:
            in_expr = pk_cols[0]
            zero_idx = True
        return self.query_info(False, True, in_expr, pk_cols, zero_idx, None)

    def init_class_attribute(self, mapper):
        self.parent_property._get_strategy(
            (("lazy", "select"),)
        ).init_class_attribute(mapper)

    def create_row_processor(
        self,
        context,
        query_entity,
        path,
        loadopt,
        mapper,
        result,
        adapter,
        populators,
    ):

        if context.refresh_state:
            return self._immediateload_create_row_processor(
                context,
                query_entity,
                path,
                loadopt,
                mapper,
                result,
                adapter,
                populators,
            )

        (
            effective_path,
            run_loader,
            execution_options,
            recursion_depth,
        ) = self._setup_for_recursion(
            context, path, loadopt, join_depth=self.join_depth
        )
        if not run_loader:
            return

        if not self.parent.class_manager[self.key].impl.supports_population:
            raise sa_exc.InvalidRequestError(
                "'%s' does not support object "
                "population - eager loading cannot be applied." % self
            )

        # a little dance here as the "path" is still something that only
        # semi-tracks the exact series of things we are loading, still not
        # telling us about with_polymorphic() and stuff like that when it's at
        # the root..  the initial MapperEntity is more accurate for this case.
        if len(path) == 1:
            if not orm_util._entity_isa(query_entity.entity_zero, self.parent):
                return
        elif not orm_util._entity_isa(path[-1], self.parent):
            return

        selectin_path = effective_path

        path_w_prop = path[self.parent_property]

        # build up a path indicating the path from the leftmost
        # entity to the thing we're subquery loading.
        with_poly_entity = path_w_prop.get(
            context.attributes, "path_with_polymorphic", None
        )
        if with_poly_entity is not None:
            effective_entity = inspect(with_poly_entity)
        else:
            effective_entity = self.entity

        loading.PostLoad.callable_for_path(
            context,
            selectin_path,
            self.parent,
            self.parent_property,
            self._load_for_path,
            effective_entity,
            loadopt,
            recursion_depth,
            execution_options,
        )

    def _load_for_path(
        self,
        context,
        path,
        states,
        load_only,
        effective_entity,
        loadopt,
        recursion_depth,
        execution_options,
    ):
        if load_only and self.key not in load_only:
            return

        query_info = self._query_info

        if query_info.load_only_child:
            our_states = collections.defaultdict(list)
            none_states = []

            mapper = self.parent

            for state, overwrite in states:
                state_dict = state.dict
                related_ident = tuple(
                    mapper._get_state_attr_by_column(
                        state,
                        state_dict,
                        lk,
                        passive=attributes.PASSIVE_NO_FETCH,
                    )
                    for lk in query_info.child_lookup_cols
                )
                # if the loaded parent objects do not have the foreign key
                # to the related item loaded, then degrade into the joined
                # version of selectinload
                if LoaderCallableStatus.PASSIVE_NO_RESULT in related_ident:
                    query_info = self._fallback_query_info
                    break

                # organize states into lists keyed to particular foreign
                # key values.
                if None not in related_ident:
                    our_states[related_ident].append(
                        (state, state_dict, overwrite)
                    )
                else:
                    # For FK values that have None, add them to a
                    # separate collection that will be populated separately
                    none_states.append((state, state_dict, overwrite))

        # note the above conditional may have changed query_info
        if not query_info.load_only_child:
            our_states = [
                (state.key[1], state, state.dict, overwrite)
                for state, overwrite in states
            ]

        pk_cols = query_info.pk_cols
        in_expr = query_info.in_expr

        if not query_info.load_with_join:
            # in "omit join" mode, the primary key column and the
            # "in" expression are in terms of the related entity.  So
            # if the related entity is polymorphic or otherwise aliased,
            # we need to adapt our "pk_cols" and "in_expr" to that
            # entity.   in non-"omit join" mode, these are against the
            # parent entity and do not need adaption.
            if effective_entity.is_aliased_class:
                pk_cols = [
                    effective_entity._adapt_element(col) for col in pk_cols
                ]
                in_expr = effective_entity._adapt_element(in_expr)

        bundle_ent = orm_util.Bundle("pk", *pk_cols)
        bundle_sql = bundle_ent.__clause_element__()

        entity_sql = effective_entity.__clause_element__()
        q = Select._create_raw_select(
            _raw_columns=[bundle_sql, entity_sql],
            _label_style=LABEL_STYLE_TABLENAME_PLUS_COL,
            _compile_options=ORMCompileState.default_compile_options,
            _propagate_attrs={
                "compile_state_plugin": "orm",
                "plugin_subject": effective_entity,
            },
        )

        if not query_info.load_with_join:
            # the Bundle we have in the "omit_join" case is against raw, non
            # annotated columns, so to ensure the Query knows its primary
            # entity, we add it explicitly.  If we made the Bundle against
            # annotated columns, we hit a performance issue in this specific
            # case, which is detailed in issue #4347.
            q = q.select_from(effective_entity)
        else:
            # in the non-omit_join case, the Bundle is against the annotated/
            # mapped column of the parent entity, but the #4347 issue does not
            # occur in this case.
            q = q.select_from(self._parent_alias).join(
                getattr(self._parent_alias, self.parent_property.key).of_type(
                    effective_entity
                )
            )

        q = q.filter(in_expr.in_(sql.bindparam("primary_keys")))

        # a test which exercises what these comments talk about is
        # test_selectin_relations.py -> test_twolevel_selectin_w_polymorphic
        #
        # effective_entity above is given to us in terms of the cached
        # statement, namely this one:
        orig_query = context.compile_state.select_statement

        # the actual statement that was requested is this one:
        #  context_query = context.query
        #
        # that's not the cached one, however.  So while it is of the identical
        # structure, if it has entities like AliasedInsp, which we get from
        # aliased() or with_polymorphic(), the AliasedInsp will likely be a
        # different object identity each time, and will not match up
        # hashing-wise to the corresponding AliasedInsp that's in the
        # cached query, meaning it won't match on paths and loader lookups
        # and loaders like this one will be skipped if it is used in options.
        #
        # as it turns out, standard loader options like selectinload(),
        # lazyload() that have a path need
        # to come from the cached query so that the AliasedInsp etc. objects
        # that are in the query line up with the object that's in the path
        # of the strategy object. however other options like
        # with_loader_criteria() that doesn't have a path (has a fixed entity)
        # and needs to have access to the latest closure state in order to
        # be correct, we need to use the uncached one.
        #
        # as of #8399 we let the loader option itself figure out what it
        # wants to do given cached and uncached version of itself.

        effective_path = path[self.parent_property]

        if orig_query is context.query:
            new_options = orig_query._with_options
        else:
            cached_options = orig_query._with_options
            uncached_options = context.query._with_options

            # propagate compile state options from the original query,
            # updating their "extra_criteria" as necessary.
            # note this will create a different cache key than
            # "orig" options if extra_criteria is present, because the copy
            # of extra_criteria will have different boundparam than that of
            # the QueryableAttribute in the path
            new_options = [
                orig_opt._adapt_cached_option_to_uncached_option(
                    context, uncached_opt
                )
                for orig_opt, uncached_opt in zip(
                    cached_options, uncached_options
                )
            ]

        if loadopt and loadopt._extra_criteria:
            new_options += (
                orm_util.LoaderCriteriaOption(
                    effective_entity,
                    loadopt._generate_extra_criteria(context),
                ),
            )

        if recursion_depth is not None:
            effective_path = effective_path._truncate_recursive()

        q = q.options(*new_options)

        q = q._update_compile_options({"_current_path": effective_path})
        if context.populate_existing:
            q = q.execution_options(populate_existing=True)

        if self.parent_property.order_by:
            if not query_info.load_with_join:
                eager_order_by = self.parent_property.order_by
                if effective_entity.is_aliased_class:
                    eager_order_by = [
                        effective_entity._adapt_element(elem)
                        for elem in eager_order_by
                    ]
                q = q.order_by(*eager_order_by)
            else:

                def _setup_outermost_orderby(compile_context):
                    compile_context.eager_order_by += tuple(
                        util.to_list(self.parent_property.order_by)
                    )

                q = q._add_context_option(
                    _setup_outermost_orderby, self.parent_property
                )

        if query_info.load_only_child:
            self._load_via_child(
                our_states,
                none_states,
                query_info,
                q,
                context,
                execution_options,
            )
        else:
            self._load_via_parent(
                our_states, query_info, q, context, execution_options
            )

    def _load_via_child(
        self,
        our_states,
        none_states,
        query_info,
        q,
        context,
        execution_options,
    ):
        uselist = self.uselist

        # this sort is really for the benefit of the unit tests
        our_keys = sorted(our_states)
        while our_keys:
            chunk = our_keys[0 : self._chunksize]
            our_keys = our_keys[self._chunksize :]
            data = {
                k: v
                for k, v in context.session.execute(
                    q,
                    params={
                        "primary_keys": [
                            key[0] if query_info.zero_idx else key
                            for key in chunk
                        ]
                    },
                    execution_options=execution_options,
                ).unique()
            }

            for key in chunk:
                # for a real foreign key and no concurrent changes to the
                # DB while running this method, "key" is always present in
                # data.  However, for primaryjoins without real foreign keys
                # a non-None primaryjoin condition may still refer to no
                # related object.
                related_obj = data.get(key, None)
                for state, dict_, overwrite in our_states[key]:
                    if not overwrite and self.key in dict_:
                        continue

                    state.get_impl(self.key).set_committed_value(
                        state,
                        dict_,
                        related_obj if not uselist else [related_obj],
                    )
        # populate none states with empty value / collection
        for state, dict_, overwrite in none_states:
            if not overwrite and self.key in dict_:
                continue

            # note it's OK if this is a uselist=True attribute, the empty
            # collection will be populated
            state.get_impl(self.key).set_committed_value(state, dict_, None)

    def _load_via_parent(
        self, our_states, query_info, q, context, execution_options
    ):
        uselist = self.uselist
        _empty_result = () if uselist else None

        while our_states:
            chunk = our_states[0 : self._chunksize]
            our_states = our_states[self._chunksize :]

            primary_keys = [
                key[0] if query_info.zero_idx else key
                for key, state, state_dict, overwrite in chunk
            ]

            data = collections.defaultdict(list)
            for k, v in itertools.groupby(
                context.session.execute(
                    q,
                    params={"primary_keys": primary_keys},
                    execution_options=execution_options,
                ).unique(),
                lambda x: x[0],
            ):
                data[k].extend(vv[1] for vv in v)

            for key, state, state_dict, overwrite in chunk:

                if not overwrite and self.key in state_dict:
                    continue

                collection = data.get(key, _empty_result)

                if not uselist and collection:
                    if len(collection) > 1:
                        util.warn(
                            "Multiple rows returned with "
                            "uselist=False for eagerly-loaded "
                            "attribute '%s' " % self
                        )
                    state.get_impl(self.key).set_committed_value(
                        state, state_dict, collection[0]
                    )
                else:
                    # note that empty tuple set on uselist=False sets the
                    # value to None
                    state.get_impl(self.key).set_committed_value(
                        state, state_dict, collection
                    )


def single_parent_validator(desc, prop):
    def _do_check(state, value, oldvalue, initiator):
        if value is not None and initiator.key == prop.key:
            hasparent = initiator.hasparent(attributes.instance_state(value))
            if hasparent and oldvalue is not value:
                raise sa_exc.InvalidRequestError(
                    "Instance %s is already associated with an instance "
                    "of %s via its %s attribute, and is only allowed a "
                    "single parent."
                    % (orm_util.instance_str(value), state.class_, prop),
                    code="bbf1",
                )
        return value

    def append(state, value, initiator):
        return _do_check(state, value, None, initiator)

    def set_(state, value, oldvalue, initiator):
        return _do_check(state, value, oldvalue, initiator)

    event.listen(
        desc, "append", append, raw=True, retval=True, active_history=True
    )
    event.listen(desc, "set", set_, raw=True, retval=True, active_history=True)
