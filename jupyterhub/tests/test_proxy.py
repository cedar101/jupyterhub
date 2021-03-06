"""Test a proxy being started before the Hub"""

import json
import os
from queue import Queue
from subprocess import Popen
from urllib.parse import urlparse, quote

from traitlets.config import Config

import pytest

from .. import orm
from .mocking import MockHub
from .test_api import api_request
from ..utils import wait_for_http_server, url_path_join as ujoin


def test_external_proxy(request, io_loop):

    auth_token = 'secret!'
    proxy_ip = '127.0.0.1'
    proxy_port = 54321
    cfg = Config()
    cfg.ConfigurableHTTPProxy.auth_token = auth_token
    cfg.ConfigurableHTTPProxy.api_url = 'http://%s:%i' % (proxy_ip, proxy_port)
    cfg.ConfigurableHTTPProxy.should_start = False

    app = MockHub.instance(config=cfg)

    def fin():
        MockHub.clear_instance()
        app.stop()

    request.addfinalizer(fin)

    # configures and starts proxy process
    env = os.environ.copy()
    env['CONFIGPROXY_AUTH_TOKEN'] = auth_token
    cmd = [
        'configurable-http-proxy',
        '--ip', app.ip,
        '--port', str(app.port),
        '--api-ip', proxy_ip,
        '--api-port', str(proxy_port),
        '--log-level=debug',
    ]
    if app.subdomain_host:
        cmd.append('--host-routing')
    proxy = Popen(cmd, env=env)


    def _cleanup_proxy():
        if proxy.poll() is None:
            proxy.terminate()
    request.addfinalizer(_cleanup_proxy)

    def wait_for_proxy():
        io_loop.run_sync(lambda: wait_for_http_server('http://%s:%i' % (proxy_ip, proxy_port)))
    wait_for_proxy()

    app.start([])
    assert app.proxy.proxy_process is None

    # test if api service has a root route '/'
    routes = io_loop.run_sync(app.proxy.get_all_routes)
    assert list(routes.keys()) == ['/']
    
    # add user to the db and start a single user server
    name = 'river'
    r = api_request(app, 'users', name, method='post')
    r.raise_for_status()
    r = api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    
    routes = io_loop.run_sync(app.proxy.get_all_routes)
    # sets the desired path result
    user_path = ujoin(app.base_url, 'user/river') + '/'
    print(app.base_url, user_path)
    host = ''
    if app.subdomain_host:
        host = '%s.%s' % (name, urlparse(app.subdomain_host).hostname)
    user_spec = host + user_path
    assert sorted(routes.keys()) == ['/', user_spec]
    
    # teardown the proxy and start a new one in the same place
    proxy.terminate()
    proxy = Popen(cmd, env=env)
    wait_for_proxy()

    routes = io_loop.run_sync(app.proxy.get_all_routes)

    assert list(routes.keys()) == []
    
    # poke the server to update the proxy
    r = api_request(app, 'proxy', method='post')
    r.raise_for_status()

    # check that the routes are correct
    routes = io_loop.run_sync(app.proxy.get_all_routes)
    assert sorted(routes.keys()) == ['/', user_spec]

    # teardown the proxy, and start a new one with different auth and port
    proxy.terminate()
    new_auth_token = 'different!'
    env['CONFIGPROXY_AUTH_TOKEN'] = new_auth_token
    proxy_port = 55432
    cmd = ['configurable-http-proxy',
        '--ip', app.ip,
        '--port', str(app.port),
        '--api-ip', proxy_ip,
        '--api-port', str(proxy_port),
        '--default-target', 'http://%s:%i' % (app.hub_ip, app.hub_port),
    ]
    if app.subdomain_host:
        cmd.append('--host-routing')
    proxy = Popen(cmd, env=env)
    wait_for_proxy()

    # tell the hub where the new proxy is
    new_api_url = 'http://{}:{}'.format(proxy_ip, proxy_port)
    r = api_request(app, 'proxy', method='patch', data=json.dumps({
        'api_url': new_api_url,
        'auth_token': new_auth_token,
    }))
    r.raise_for_status()
    assert app.proxy.api_url == new_api_url

    # get updated auth token from main thread
    def get_app_proxy_token():
        q = Queue()
        app.io_loop.add_callback(lambda: q.put(app.proxy.auth_token))
        return q.get(timeout=2)

    assert get_app_proxy_token() == new_auth_token
    app.proxy.auth_token = new_auth_token

    # check that the routes are correct
    routes = io_loop.run_sync(app.proxy.get_all_routes)
    assert sorted(routes.keys()) == ['/', user_spec]


@pytest.mark.parametrize("username, endpoints", [
    ('zoe', ['users/zoe', 'users/zoe/server']),
    ('50fia', ['users/50fia', 'users/50fia/server']),
    ('秀樹', ['users/秀樹', 'users/秀樹/server']),
])
def test_check_routes(app, io_loop, username, endpoints):
    proxy = app.proxy

    for endpoint in endpoints:
        r = api_request(app, endpoint, method='post')
        r.raise_for_status()

    test_user = orm.User.find(app.db, username)
    assert test_user is not None

    # check a valid route exists for user
    test_user = app.users[username]
    before = sorted(io_loop.run_sync(app.proxy.get_all_routes))
    assert test_user.proxy_spec in before

    # check if a route is removed when user deleted
    io_loop.run_sync(lambda: app.proxy.check_routes(app.users, app._service_map))
    io_loop.run_sync(lambda: proxy.delete_user(test_user))
    during = sorted(io_loop.run_sync(app.proxy.get_all_routes))
    assert test_user.proxy_spec not in during

    # check if a route exists for user
    io_loop.run_sync(lambda: app.proxy.check_routes(app.users, app._service_map))
    after = sorted(io_loop.run_sync(app.proxy.get_all_routes))
    assert test_user.proxy_spec in after

    # check that before and after state are the same
    assert before == after

from contextlib import contextmanager

@pytest.mark.gen_test
@pytest.mark.parametrize("routespec", [
    '/has%20space/foo/',
    '/missing-trailing/slash',
    '/has/@/',
    '/has/' + quote('üñîçø∂é'),
    'host.name/path/',
    'other.host/path/no/slash',
])
def test_add_get_delete(app, routespec):
    arg = routespec
    if not routespec.endswith('/'):
        routespec = routespec + '/'
    
    # host-routes when not host-routing raises an error
    # and vice versa
    expect_value_error = bool(app.subdomain_host) ^ (not routespec.startswith('/'))
    @contextmanager
    def context():
        if expect_value_error:
            with pytest.raises(ValueError):
                yield
        else:
            yield

    proxy = app.proxy
    target = 'https://localhost:1234'
    with context():
        yield proxy.add_route(arg, target, {})
    routes = yield proxy.get_all_routes()
    if not expect_value_error:
        assert routespec in routes.keys()
    with context():
        route = yield proxy.get_route(arg)
        assert route == {
            'target': target,
            'routespec': routespec,
            'data': route.get('data'),
        }
    with context():
        yield proxy.delete_route(arg)
    with context():
        route = yield proxy.get_route(arg)
        assert route is None


@pytest.mark.parametrize("test_data", [None, 'notjson', json.dumps([])])
def test_proxy_patch_bad_request_data(app, test_data):
    r = api_request(app, 'proxy', method='patch', data=test_data)
    assert r.status_code == 400
