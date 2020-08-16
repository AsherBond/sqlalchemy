try:
    # installed by bootstrap.py
    import sqla_plugin_base as plugin_base
except ImportError:
    # assume we're a package, use traditional import
    from . import plugin_base

import argparse
import collections
from functools import update_wrapper
import inspect
import itertools
import operator
import os
import re
import sys

import pytest

try:
    import typing
except ImportError:
    pass
else:
    if typing.TYPE_CHECKING:
        from typing import Sequence

try:
    import xdist  # noqa

    has_xdist = True
except ImportError:
    has_xdist = False


def pytest_addoption(parser):
    group = parser.getgroup("sqlalchemy")

    def make_option(name, **kw):
        callback_ = kw.pop("callback", None)
        if callback_:

            class CallableAction(argparse.Action):
                def __call__(
                    self, parser, namespace, values, option_string=None
                ):
                    callback_(option_string, values, parser)

            kw["action"] = CallableAction

        zeroarg_callback = kw.pop("zeroarg_callback", None)
        if zeroarg_callback:

            class CallableAction(argparse.Action):
                def __init__(
                    self,
                    option_strings,
                    dest,
                    default=False,
                    required=False,
                    help=None,  # noqa
                ):
                    super(CallableAction, self).__init__(
                        option_strings=option_strings,
                        dest=dest,
                        nargs=0,
                        const=True,
                        default=default,
                        required=required,
                        help=help,
                    )

                def __call__(
                    self, parser, namespace, values, option_string=None
                ):
                    zeroarg_callback(option_string, values, parser)

            kw["action"] = CallableAction

        group.addoption(name, **kw)

    plugin_base.setup_options(make_option)
    plugin_base.read_config()


def pytest_configure(config):
    if hasattr(config, "workerinput"):
        plugin_base.restore_important_follower_config(config.workerinput)
        plugin_base.configure_follower(config.workerinput["follower_ident"])
    else:
        if config.option.write_idents and os.path.exists(
            config.option.write_idents
        ):
            os.remove(config.option.write_idents)

    plugin_base.pre_begin(config.option)

    plugin_base.set_coverage_flag(
        bool(getattr(config.option, "cov_source", False))
    )

    plugin_base.set_fixture_functions(PytestFixtureFunctions)

    if config.option.dump_pyannotate:
        global DUMP_PYANNOTATE
        DUMP_PYANNOTATE = True


DUMP_PYANNOTATE = False


@pytest.fixture(autouse=True)
def collect_types_fixture():
    if DUMP_PYANNOTATE:
        from pyannotate_runtime import collect_types

        collect_types.start()
    yield
    if DUMP_PYANNOTATE:
        collect_types.stop()


def pytest_sessionstart(session):
    from sqlalchemy.testing import asyncio

    asyncio._assume_async(plugin_base.post_begin)


def pytest_sessionfinish(session):
    from sqlalchemy.testing import asyncio

    asyncio._maybe_async_provisioning(plugin_base.final_process_cleanup)

    if session.config.option.dump_pyannotate:
        from pyannotate_runtime import collect_types

        collect_types.dump_stats(session.config.option.dump_pyannotate)


def pytest_collection_finish(session):
    if session.config.option.dump_pyannotate:
        from pyannotate_runtime import collect_types

        lib_sqlalchemy = os.path.abspath("lib/sqlalchemy")

        def _filter(filename):
            filename = os.path.normpath(os.path.abspath(filename))
            if "lib/sqlalchemy" not in os.path.commonpath(
                [filename, lib_sqlalchemy]
            ):
                return None
            if "testing" in filename:
                return None

            return filename

        collect_types.init_types_collection(filter_filename=_filter)


if has_xdist:
    import uuid

    def pytest_configure_node(node):
        from sqlalchemy.testing import provision
        from sqlalchemy.testing import asyncio

        # the master for each node fills workerinput dictionary
        # which pytest-xdist will transfer to the subprocess

        plugin_base.memoize_important_follower_config(node.workerinput)

        node.workerinput["follower_ident"] = "test_%s" % uuid.uuid4().hex[0:12]

        asyncio._maybe_async_provisioning(
            provision.create_follower_db, node.workerinput["follower_ident"]
        )

    def pytest_testnodedown(node, error):
        from sqlalchemy.testing import provision
        from sqlalchemy.testing import asyncio

        asyncio._maybe_async_provisioning(
            provision.drop_follower_db, node.workerinput["follower_ident"]
        )


def pytest_collection_modifyitems(session, config, items):

    # look for all those classes that specify __backend__ and
    # expand them out into per-database test cases.

    # this is much easier to do within pytest_pycollect_makeitem, however
    # pytest is iterating through cls.__dict__ as makeitem is
    # called which causes a "dictionary changed size" error on py3k.
    # I'd submit a pullreq for them to turn it into a list first, but
    # it's to suit the rather odd use case here which is that we are adding
    # new classes to a module on the fly.

    from sqlalchemy.testing import asyncio

    rebuilt_items = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )

    items[:] = [
        item
        for item in items
        if isinstance(item.parent, pytest.Instance)
        and not item.parent.parent.name.startswith("_")
    ]

    test_classes = set(item.parent for item in items)

    def setup_test_classes():
        for test_class in test_classes:
            for sub_cls in plugin_base.generate_sub_tests(
                test_class.cls, test_class.parent.module
            ):
                if sub_cls is not test_class.cls:
                    per_cls_dict = rebuilt_items[test_class.cls]

                    # support pytest 5.4.0 and above pytest.Class.from_parent
                    ctor = getattr(pytest.Class, "from_parent", pytest.Class)
                    for inst in ctor(
                        name=sub_cls.__name__, parent=test_class.parent.parent
                    ).collect():
                        for t in inst.collect():
                            per_cls_dict[t.name].append(t)

    # class requirements will sometimes need to access the DB to check
    # capabilities, so need to do this for async
    asyncio._maybe_async_provisioning(setup_test_classes)

    newitems = []
    for item in items:
        if item.parent.cls in rebuilt_items:
            newitems.extend(rebuilt_items[item.parent.cls][item.name])
        else:
            newitems.append(item)

    # seems like the functions attached to a test class aren't sorted already?
    # is that true and why's that? (when using unittest, they're sorted)
    items[:] = sorted(
        newitems,
        key=lambda item: (
            item.parent.parent.parent.name,
            item.parent.parent.name,
            item.name,
        ),
    )


def pytest_pycollect_makeitem(collector, name, obj):

    if inspect.isclass(obj) and plugin_base.want_class(name, obj):
        from sqlalchemy.testing import config

        if config.any_async and getattr(obj, "__asyncio_wrap__", True):
            obj = _apply_maybe_async(obj)

        ctor = getattr(pytest.Class, "from_parent", pytest.Class)

        return [
            ctor(name=parametrize_cls.__name__, parent=collector)
            for parametrize_cls in _parametrize_cls(collector.module, obj)
        ]
    elif (
        inspect.isfunction(obj)
        and isinstance(collector, pytest.Instance)
        and plugin_base.want_method(collector.cls, obj)
    ):
        # None means, fall back to default logic, which includes
        # method-level parametrize
        return None
    else:
        # empty list means skip this item
        return []


def _apply_maybe_async(obj, recurse=True):
    from sqlalchemy.testing import asyncio

    setup_names = {"setup", "setup_class", "teardown", "teardown_class"}
    for name, value in vars(obj).items():
        if (
            (callable(value) or isinstance(value, classmethod))
            and not getattr(value, "_maybe_async_applied", False)
            and (name.startswith("test_") or name in setup_names)
        ):
            is_classmethod = False
            if isinstance(value, classmethod):
                value = value.__func__
                is_classmethod = True

            @_pytest_fn_decorator
            def make_async(fn, *args, **kwargs):
                return asyncio._maybe_async(fn, *args, **kwargs)

            do_async = make_async(value)
            if is_classmethod:
                do_async = classmethod(do_async)
            do_async._maybe_async_applied = True

            setattr(obj, name, do_async)
    if recurse:
        for cls in obj.mro()[1:]:
            if cls != object:
                _apply_maybe_async(cls, False)
    return obj


_current_class = None


def _parametrize_cls(module, cls):
    """implement a class-based version of pytest parametrize."""

    if "_sa_parametrize" not in cls.__dict__:
        return [cls]

    _sa_parametrize = cls._sa_parametrize
    classes = []
    for full_param_set in itertools.product(
        *[params for argname, params in _sa_parametrize]
    ):
        cls_variables = {}

        for argname, param in zip(
            [_sa_param[0] for _sa_param in _sa_parametrize], full_param_set
        ):
            if not argname:
                raise TypeError("need argnames for class-based combinations")
            argname_split = re.split(r",\s*", argname)
            for arg, val in zip(argname_split, param.values):
                cls_variables[arg] = val
        parametrized_name = "_".join(
            # token is a string, but in py2k pytest is giving us a unicode,
            # so call str() on it.
            str(re.sub(r"\W", "", token))
            for param in full_param_set
            for token in param.id.split("-")
        )
        name = "%s_%s" % (cls.__name__, parametrized_name)
        newcls = type.__new__(type, name, (cls,), cls_variables)
        setattr(module, name, newcls)
        classes.append(newcls)
    return classes


def pytest_runtest_setup(item):
    from sqlalchemy.testing import asyncio

    # here we seem to get called only based on what we collected
    # in pytest_collection_modifyitems.   So to do class-based stuff
    # we have to tear that out.
    global _current_class

    if not isinstance(item, pytest.Function):
        return

    # ... so we're doing a little dance here to figure it out...
    if _current_class is None:
        asyncio._maybe_async(class_setup, item.parent.parent)
        _current_class = item.parent.parent

        # this is needed for the class-level, to ensure that the
        # teardown runs after the class is completed with its own
        # class-level teardown...
        def finalize():
            global _current_class
            asyncio._maybe_async(class_teardown, item.parent.parent)
            _current_class = None

        item.parent.parent.addfinalizer(finalize)

    asyncio._maybe_async(test_setup, item)


def pytest_runtest_teardown(item):
    from sqlalchemy.testing import asyncio

    # ...but this works better as the hook here rather than
    # using a finalizer, as the finalizer seems to get in the way
    # of the test reporting failures correctly (you get a bunch of
    # pytest assertion stuff instead)
    asyncio._maybe_async(test_teardown, item)


def test_setup(item):
    plugin_base.before_test(
        item, item.parent.module.__name__, item.parent.cls, item.name
    )


def test_teardown(item):
    plugin_base.after_test(item)


def class_setup(item):
    from sqlalchemy.testing import asyncio

    asyncio._maybe_async_provisioning(plugin_base.start_test_class, item.cls)


def class_teardown(item):
    plugin_base.stop_test_class(item.cls)


def getargspec(fn):
    if sys.version_info.major == 3:
        return inspect.getfullargspec(fn)
    else:
        return inspect.getargspec(fn)


def _pytest_fn_decorator(target):
    """Port of langhelpers.decorator with pytest-specific tricks."""

    from sqlalchemy.util.langhelpers import format_argspec_plus
    from sqlalchemy.util.compat import inspect_getfullargspec

    def _exec_code_in_env(code, env, fn_name):
        exec(code, env)
        return env[fn_name]

    def decorate(fn, add_positional_parameters=()):

        spec = inspect_getfullargspec(fn)
        if add_positional_parameters:
            spec.args.extend(add_positional_parameters)

        metadata = dict(
            __target_fn="__target_fn", __orig_fn="__orig_fn", name=fn.__name__
        )
        metadata.update(format_argspec_plus(spec, grouped=False))
        code = (
            """\
def %(name)s(%(args)s):
    return %(__target_fn)s(%(__orig_fn)s, %(apply_kw)s)
"""
            % metadata
        )
        decorated = _exec_code_in_env(
            code, {"__target_fn": target, "__orig_fn": fn}, fn.__name__
        )
        if not add_positional_parameters:
            decorated.__defaults__ = getattr(fn, "__func__", fn).__defaults__
            decorated.__wrapped__ = fn
            return update_wrapper(decorated, fn)
        else:
            # this is the pytest hacky part.  don't do a full update wrapper
            # because pytest is really being sneaky about finding the args
            # for the wrapped function
            decorated.__module__ = fn.__module__
            decorated.__name__ = fn.__name__
            return decorated

    return decorate


class PytestFixtureFunctions(plugin_base.FixtureFunctions):
    def skip_test_exception(self, *arg, **kw):
        return pytest.skip.Exception(*arg, **kw)

    _combination_id_fns = {
        "i": lambda obj: obj,
        "r": repr,
        "s": str,
        "n": operator.attrgetter("__name__"),
    }

    def combinations(self, *arg_sets, **kw):
        """Facade for pytest.mark.parametrize.

        Automatically derives argument names from the callable which in our
        case is always a method on a class with positional arguments.

        ids for parameter sets are derived using an optional template.

        """
        from sqlalchemy.testing import exclusions

        if sys.version_info.major == 3:
            if len(arg_sets) == 1 and hasattr(arg_sets[0], "__next__"):
                arg_sets = list(arg_sets[0])
        else:
            if len(arg_sets) == 1 and hasattr(arg_sets[0], "next"):
                arg_sets = list(arg_sets[0])

        argnames = kw.pop("argnames", None)

        def _filter_exclusions(args):
            result = []
            gathered_exclusions = []
            for a in args:
                if isinstance(a, exclusions.compound):
                    gathered_exclusions.append(a)
                else:
                    result.append(a)

            return result, gathered_exclusions

        id_ = kw.pop("id_", None)

        tobuild_pytest_params = []
        has_exclusions = False
        if id_:
            _combination_id_fns = self._combination_id_fns

            # because itemgetter is not consistent for one argument vs.
            # multiple, make it multiple in all cases and use a slice
            # to omit the first argument
            _arg_getter = operator.itemgetter(
                0,
                *[
                    idx
                    for idx, char in enumerate(id_)
                    if char in ("n", "r", "s", "a")
                ]
            )
            fns = [
                (operator.itemgetter(idx), _combination_id_fns[char])
                for idx, char in enumerate(id_)
                if char in _combination_id_fns
            ]

            for arg in arg_sets:
                if not isinstance(arg, tuple):
                    arg = (arg,)

                fn_params, param_exclusions = _filter_exclusions(arg)

                parameters = _arg_getter(fn_params)[1:]

                if param_exclusions:
                    has_exclusions = True

                tobuild_pytest_params.append(
                    (
                        parameters,
                        param_exclusions,
                        "-".join(
                            comb_fn(getter(arg)) for getter, comb_fn in fns
                        ),
                    )
                )

        else:

            for arg in arg_sets:
                if not isinstance(arg, tuple):
                    arg = (arg,)

                fn_params, param_exclusions = _filter_exclusions(arg)

                if param_exclusions:
                    has_exclusions = True

                tobuild_pytest_params.append(
                    (fn_params, param_exclusions, None)
                )

        pytest_params = []
        for parameters, param_exclusions, id_ in tobuild_pytest_params:
            if has_exclusions:
                parameters += (param_exclusions,)

            param = pytest.param(*parameters, id=id_)
            pytest_params.append(param)

        def decorate(fn):
            if inspect.isclass(fn):
                if has_exclusions:
                    raise NotImplementedError(
                        "exclusions not supported for class level combinations"
                    )
                if "_sa_parametrize" not in fn.__dict__:
                    fn._sa_parametrize = []
                fn._sa_parametrize.append((argnames, pytest_params))
                return fn
            else:
                if argnames is None:
                    _argnames = getargspec(fn).args[1:]  # type: Sequence(str)
                else:
                    _argnames = re.split(
                        r", *", argnames
                    )  # type: Sequence(str)

                if has_exclusions:
                    _argnames += ["_exclusions"]

                    @_pytest_fn_decorator
                    def check_exclusions(fn, *args, **kw):
                        _exclusions = args[-1]
                        if _exclusions:
                            exlu = exclusions.compound().add(*_exclusions)
                            fn = exlu(fn)
                        return fn(*args[0:-1], **kw)

                    def process_metadata(spec):
                        spec.args.append("_exclusions")

                    fn = check_exclusions(
                        fn, add_positional_parameters=("_exclusions",)
                    )

                return pytest.mark.parametrize(_argnames, pytest_params)(fn)

        return decorate

    def param_ident(self, *parameters):
        ident = parameters[0]
        return pytest.param(*parameters[1:], id=ident)

    def fixture(self, *arg, **kw):
        from sqlalchemy.testing import config
        from sqlalchemy.testing import asyncio

        # wrapping pytest.fixture function.  determine if
        # decorator was called as @fixture or @fixture().
        if len(arg) > 0 and callable(arg[0]):
            # was called as @fixture(), we have the function to wrap.
            fn = arg[0]
            arg = arg[1:]
        else:
            # was called as @fixture, don't have the function yet.
            fn = None

        # create a pytest.fixture marker.  because the fn is not being
        # passed, this is always a pytest.FixtureFunctionMarker()
        # object (or whatever pytest is calling it when you read this)
        # that is waiting for a function.
        fixture = pytest.fixture(*arg, **kw)

        # now apply wrappers to the function, including fixture itself

        def wrap(fn):
            if config.any_async:
                fn = asyncio._maybe_async_wrapper(fn)
            # other wrappers may be added here

            # now apply FixtureFunctionMarker
            fn = fixture(fn)
            return fn

        if fn:
            return wrap(fn)
        else:
            return wrap

    def get_current_test_name(self):
        return os.environ.get("PYTEST_CURRENT_TEST")

    def async_test(self, fn):
        from sqlalchemy.testing import asyncio

        @_pytest_fn_decorator
        def decorate(fn, *args, **kwargs):
            asyncio._assume_async(fn, *args, **kwargs)

        return decorate(fn)
