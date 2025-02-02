import traceback

import pytest
import six
from mock import ANY, patch
from nameko.exceptions import RemoteError
from nameko.testing.services import entrypoint_waiter, get_extension
from nameko_amqp_retry import Backoff
from nameko_amqp_retry.rpc import RpcProxy, Rpc, rpc
from test import PY3, PY34


class TestRpc(object):

    def test_rpc(
        self, container, entrypoint_tracker, rpc_proxy, wait_for_result,
        backoff_count
    ):
        """ RPC entrypoint supports backoff
        """
        with entrypoint_waiter(
            container, 'method', callback=wait_for_result
        ) as result:
            res = rpc_proxy.service.method("arg")

        assert res == result.get() == "result"

        # entrypoint fired backoff_count + 1 times
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["result"]
        )
        # entrypoint raised `Backoff` for all but the last execution
        assert entrypoint_tracker.get_exceptions() == (
            [(Backoff, ANY, ANY)] * backoff_count + [None]
        )

    def test_expiry(
        self, container, entrypoint_tracker, rpc_proxy, limited_backoff,
        wait_for_backoff_expired
    ):
        """ RPC entrypoint supports backoff expiry
        """
        with entrypoint_waiter(
            container, 'method', callback=wait_for_backoff_expired
        ) as result:
            with pytest.raises(RemoteError) as raised:
                rpc_proxy.service.method("arg")
            assert raised.value.exc_type == "Expired"

        with pytest.raises(Backoff.Expired) as raised:
            result.get()
        assert (
            "Backoff aborted after '{}' retries".format(limited_backoff)
        ) in str(raised.value)

        # entrypoint fired `limited_backoff` + 1 times
        assert entrypoint_tracker.get_results() == (
            [None] * limited_backoff + [None]
        )
        # entrypoint raised `Backoff` for all but the last execution,
        # and then raised `Backoff.Expired`
        assert entrypoint_tracker.get_exceptions() == (
            [(Backoff, ANY, ANY)] * limited_backoff +
            [(Backoff.Expired, ANY, ANY)]
        )

    def test_chain_backoff_exception(
        self, container_factory, rabbit_config, rpc_proxy, backoff_count,
        counter, entrypoint_tracker, wait_for_result
    ):
        """ Backoff can be chained to a root-cause exception
        """
        class NotYet(Exception):
            pass

        class Service(object):
            name = "service"

            @rpc
            def method(self, arg):
                try:
                    if counter.increment() <= backoff_count:
                        raise NotYet("try again later")
                except NotYet as exc:
                    six.raise_from(Backoff(), exc)
                return "result"

        container = container_factory(Service, rabbit_config)
        container.start()

        with entrypoint_waiter(
            container, 'method', callback=wait_for_result
        ) as result:
            res = rpc_proxy.service.method("arg")

        assert res == result.get() == "result"

        # entrypoint fired backoff_count + 1 times
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["result"]
        )
        # entrypoint raised `Backoff` for all but the last execution
        assert entrypoint_tracker.get_exceptions() == (
            [(Backoff, ANY, ANY)] * backoff_count + [None]
        )

        # on py3, backoff contains chained exception
        if PY3:
            exc_type, exc, tb = entrypoint_tracker.get_exceptions()[0]
            stack = "".join(traceback.format_exception(exc_type, exc, tb))
            assert "NotYet: try again later" in stack
            assert "nameko_amqp_retry.backoff.Backoff" in stack

    def test_chain_backoff_expired(
        self, container_factory, rabbit_config, rpc_proxy, limited_backoff,
        counter, entrypoint_tracker, wait_for_backoff_expired
    ):
        """ Backoff.Expired can be chained to a Backoff exception and
        root-cause exception
        """
        class NotYet(Exception):
            pass

        class Service(object):
            name = "service"

            @rpc
            def method(self, arg):
                try:
                    raise NotYet("try again later")
                except NotYet as exc:
                    six.raise_from(Backoff(), exc)
                return "result"

        container = container_factory(Service, rabbit_config)
        container.start()

        with entrypoint_waiter(
            container, 'method', callback=wait_for_backoff_expired
        ) as result:
            with pytest.raises(RemoteError) as raised:
                rpc_proxy.service.method("arg")
            assert raised.value.exc_type == "Expired"

        with pytest.raises(Backoff.Expired) as raised:
            result.get()
        assert (
            "Backoff aborted after '{}' retries".format(limited_backoff)
        ) in str(raised.value)

        # entrypoint fired `limited_backoff` + 1 times
        assert entrypoint_tracker.get_results() == (
            [None] * limited_backoff + [None]
        )
        # entrypoint raised `Backoff` for all but the last execution,
        # and then raised `Backoff.Expired`
        assert entrypoint_tracker.get_exceptions() == (
            [(Backoff, ANY, ANY)] * limited_backoff +
            [(Backoff.Expired, ANY, ANY)]
        )

        # on py3, backoff expired contains chained exceptions
        if PY3:
            exc_type, exc, tb = entrypoint_tracker.get_exceptions()[-1]
            stack = "".join(traceback.format_exception(exc_type, exc, tb))
            assert "NotYet: try again later" in stack
            assert "nameko_amqp_retry.backoff.Backoff" in stack
            if PY34:
                assert "nameko_amqp_retry.backoff.Expired" in stack
            else:
                assert "nameko_amqp_retry.backoff.Backoff.Expired" in stack

    def test_multiple_services(
        self, rpc_proxy, wait_for_result, counter, backoff_count,
        container_factory, rabbit_config, entrypoint_tracker
    ):
        """ RPC backoff works correctly when multiple services use it
        """
        class ServiceOne(object):
            name = "service_one"

            @rpc
            def method(self, payload):
                if counter["one"].increment() <= backoff_count:
                    raise Backoff()
                return "one"

        class ServiceTwo(object):
            name = "service_two"

            @rpc
            def method(self, payload):
                if counter["two"].increment() <= backoff_count:
                    raise Backoff()
                return "two"

        container_one = container_factory(ServiceOne, rabbit_config)
        container_one.start()
        container_two = container_factory(ServiceTwo, rabbit_config)
        container_two.start()

        with entrypoint_waiter(
            container_one, 'method', callback=wait_for_result
        ) as result:
            res = rpc_proxy.service_one.method("arg")
        assert result.get() == res == "one"
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["one"]
        )
        assert counter['one'] == backoff_count + 1

        with entrypoint_waiter(
            container_two, 'method', callback=wait_for_result
        ) as result:
            res = rpc_proxy.service_two.method("arg")
        assert result.get() == res == "two"
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["one"] +
            [None] * backoff_count + ["two"]
        )
        assert counter['two'] == backoff_count + 1

    def test_multiple_services_rpc_proxy(
        self, rpc_proxy, wait_for_result, counter, backoff_count,
        container_factory, rabbit_config, entrypoint_tracker
    ):
        """ RPC backoff works correctly when multiple services use it
        """
        class ServiceOne(object):
            name = "service_one"

            @rpc
            def method(self, payload):
                if counter[payload].increment() <= backoff_count:
                    raise Backoff()
                return payload

        class ServiceTwo(object):
            name = "service_two"

            service_one = RpcProxy("service_one")

            @rpc
            def method(self, payload):
                if counter[payload].increment() <= backoff_count:
                    raise Backoff()
                return self.service_one.method("three")

        container_one = container_factory(ServiceOne, rabbit_config)
        container_one.start()
        container_two = container_factory(ServiceTwo, rabbit_config)
        container_two.start()

        with entrypoint_waiter(
            container_one, 'method', callback=wait_for_result
        ) as result:
            res = rpc_proxy.service_one.method("one")
        assert result.get() == res == "one"
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["one"]
        )
        assert counter['one'] == backoff_count + 1

        with entrypoint_waiter(
            container_two, 'method', callback=wait_for_result
        ) as result:
            res = rpc_proxy.service_two.method("two")
        assert result.get() == res == "three"
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["one"] +
            [None] * backoff_count + ["three"]
        )
        assert counter['two'] == backoff_count + 1
        assert counter['three'] == backoff_count + 1

    def test_multiple_methods(
        self, container_factory, rabbit_config, wait_for_result, rpc_proxy,
        entrypoint_tracker, counter, backoff_count
    ):
        """ RPC backoff works correctly when multiple entrypoints in the same
        service use it
        """
        class Service(object):
            name = "service"

            @rpc
            def a(self):
                if counter["a"].increment() <= backoff_count:
                    raise Backoff()
                return "a"

            @rpc
            def b(self):
                if counter["b"].increment() <= backoff_count:
                    raise Backoff()
                return "b"

        container = container_factory(Service, rabbit_config)
        container.start()

        with entrypoint_waiter(container, 'a', callback=wait_for_result):
            rpc_proxy.service.a()
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["a"]
        )
        assert counter['a'] == backoff_count + 1

        with entrypoint_waiter(container, 'b', callback=wait_for_result):
            rpc_proxy.service.b()
        assert entrypoint_tracker.get_results() == (
            [None] * backoff_count + ["a"] +
            [None] * backoff_count + ["b"]
        )
        assert counter['b'] == backoff_count + 1

    def test_non_backoff_exception(
        self, container_factory, rabbit_config, rpc_proxy
    ):
        """ Non-backoff exceptions are handled normally
        """
        class Boom(Exception):
            pass

        class Service(object):
            name = "service"

            @rpc
            def method(self):
                raise Boom()

        container = container_factory(Service, rabbit_config)
        container.start()

        with pytest.raises(RemoteError) as exc_info:
            rpc_proxy.service.method()
        assert exc_info.value.exc_type == "Boom"

    @patch.object(Rpc, 'handle_message')
    def test_error_during_handle_message(
        self, patched_handle_message, container_factory, rabbit_config,
        rpc_proxy
    ):
        """ Backoff doesn't interfere with error handling in
        RpcConsumer.handle_message
        """
        class Boom(Exception):
            pass

        class Service(object):
            name = "service"

            @rpc
            def method(self):
                return "OK"

        patched_handle_message.side_effect = Boom

        container = container_factory(Service, rabbit_config)
        container.start()

        with pytest.raises(RemoteError) as exc_info:
            rpc_proxy.service.method()
        assert exc_info.value.exc_type == "Boom"


class TestExpectedExceptions(object):

    class UserException(Exception):
        pass

    @pytest.fixture
    def container(self, container_factory):

        class Service(object):
            name = "service"

            @rpc
            def nothing_expected(self):
                raise Backoff()

            @rpc(expected_exceptions=self.UserException)
            def something_expected(self):
                raise Backoff()

            @rpc(expected_exceptions=(self.UserException, Backoff))
            def backoff_expected(self):
                raise Backoff()

        config = {'AMQP_URI': 'memory://localhost'}
        container = container_factory(Service, config)
        return container

    def test_without_user_specified_exceptions(self, container):
        nothing_expected = get_extension(
            container, Rpc, method_name="nothing_expected"
        )
        assert issubclass(Backoff, nothing_expected.expected_exceptions)

    def test_with_user_specified_exceptions(self, container):
        something_expected = get_extension(
            container, Rpc, method_name="something_expected"
        )
        assert issubclass(
            self.UserException, something_expected.expected_exceptions
        )
        assert issubclass(Backoff, something_expected.expected_exceptions)

    def test_with_user_specified_exceptions_including_backoff(self, container):
        backoff_expected = get_extension(
            container, Rpc, method_name="backoff_expected"
        )
        assert issubclass(
            self.UserException, backoff_expected.expected_exceptions
        )
        assert issubclass(Backoff, backoff_expected.expected_exceptions)
