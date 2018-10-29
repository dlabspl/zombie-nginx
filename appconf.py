import sys
from collections import defaultdict

import yaml


_GZIP_TYPES = [
    'text/plain', 'text/xml', 'text/css', 'application/x-javascript', 'application/javascript',
    'application/ecmascript', 'application/rss+xml', 'application/xml', 'application/json',
]

_HTTP_HEADERS = {
    'X-Frame-Options': 'sameorigin',
    'X-Content-Type-Options': 'nosniff',
    'X-XSS-Protection': '"1; mode=block"',
}

_HTTPS_HEADERS = {
    'Strict-Transport-Security': 'max-age=63072000',
}
_HTTPS_HEADERS.update(_HTTP_HEADERS)

_TLS_CIPHERS = [
    'ECDHE-RSA-CHACHA20-POLY1305',
    'ECDHE-RSA-AES256-GCM-SHA512',
    'ECDHE-RSA-AES256-GCM-SHA384',
    'ECDHE-RSA-AES128-GCM-SHA256',
    'DHE-RSA-AES256-GCM-SHA512',
    'DHE-RSA-AES256-GCM-SHA384',
    'DHE-RSA-AES128-GCM-SHA256',
    'ECDHE-RSA-AES256-SHA384',
    'ECDHE-RSA-AES128-SHA256',
]

_NGINX_GLOBALS = [
    ('#', 'Auto-generated by Zombie Nginx configurator'),
    ('user', 'nginx'),
    ('worker_processes', 'auto'),
    ('worker_cpu_affinity', 'auto'),
    ('error_log', '/var/log/nginx/error.log', 'warn'),
    ('pid', '/var/run/nginx.pid'),
    ('events', [
        ('worker_connections', '2048'),
        ('multi_accept', 'on'),
    ])
]

_NGINX_HTTP = [
    ('server_tokens', 'off'),

    ('include', '/etc/nginx/mime.types'),
    ('default_type', 'application/octet-stream'),

    ('log_format', 'main',
        '\'$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" '
        '"$http_user_agent" "$request_time $upstream_connect_time $upstream_header_time $upstream_response_time" '
        '$upstream_addr $upstream_status$ssl_protocol/$ssl_session_reused/$ssl_cipher '
        '$connection/$connection_requests $gzip_ratio $request_id\''),
    ('access_log', '/var/log/nginx/access.log', 'main', 'buffer=64k', 'flush=3s'),
    ('error_log', '/var/log/nginx/error.log', 'warn'),

    ('gzip', 'on'),
    ('gzip_min_length', '1000'),
    ('gzip_static', 'on'),
    ('gzip_types', *_GZIP_TYPES),
    ('gzip_vary', 'on'),

    ('sendfile', 'on'),
    ('tcp_nopush', 'on'),
    ('tcp_nodelay', 'on'),
    ('keepalive_timeout', '65'),
    ('keepalive_requests', '1000'),
    ('postpone_output', '1460'),
    ('reset_timedout_connection', 'on'),

    ('open_file_cache', 'max=10000', 'inactive=20s'),
    ('open_file_cache_valid', '30s'),
    ('open_file_cache_min_uses', '2'),
    ('open_file_cache_errors', 'on'),
]


def _base_config_http_common(server_name, strict_host):
    config = [
        ('listen', '80', 'deferred', 'reuseport'),
        ('listen', '[::]:80', 'deferred', 'reuseport'),
        ('server_name', server_name),
    ]
    if strict_host:
        config.append(('if', f'($http_host !~* ^{server_name}$)', [('return', '444')]))
    return config


def base_config_https_redirect(server_name, strict_host):
    return _base_config_http_common(server_name, strict_host) + [
        ('return', '301', f'https://{server_name}$request_uri'),
    ]


def base_config_http(server_name, strict_host):
    return _base_config_http_common(server_name, strict_host) + [
        ('add_header', name, value, 'always') for name, value in _HTTP_HEADERS.items()
    ]


def base_config_https(server_name, strict_host):
    return [
        ('listen', '443', 'ssl', 'http2', 'deferred', 'reuseport'),
        ('listen', '[::]:443', 'ssl', 'http2', 'deferred', 'reuseport'),
        ('server_name', server_name),
    ] + ([('if', f'($http_host !~* ^{server_name}$)', [('return', '444')])] if strict_host else []) + [
        ('add_header', name, value, 'always') for name, value in _HTTPS_HEADERS.items()
    ] + [
        ('ssl_protocols', 'TLSv1.2'),

        ('ssl_prefer_server_ciphers', 'on'),
        ('ssl_ciphers', ':'.join(_TLS_CIPHERS)),
        ('ssl_dhparam', '/etc/ssl/dhparam-2048.pem'),

        ('ssl_session_timeout', '1d'),
        ('ssl_session_cache', 'shared:SSL:50m'),
        ('ssl_session_tickets', 'off'),

        ('ssl_stapling', 'on'),
        ('ssl_stapling_verify', 'on'),
        ('resolver', '8.8.8.8 8.8.4.4', 'valid=300s'),
        ('resolver_timeout', '5s'),
    ]


def emit_nginx_conf(config, *, indent=0):
    white = ' ' * indent
    for item in config:
        if isinstance(item[-1], list):
            print(white + ' '.join(item[0:-1]) + ' {')
            emit_nginx_conf(item[-1], indent=indent + 2)
            print(white + '}')
        elif item[0] == '#':
            print(white + ' '.join(item))
        else:
            print(white + ' '.join(item) + ';')


def generate_static_files_entry(description):
    if isinstance(description, str):
        description = {'path': description, 'location': '/static'}

    if 'location' not in description:
        raise Exception('location is required for static files')
    if 'path' not in description:
        raise Exception('path is required for static files')
    spa = None
    if 'spa' in description:
        spa = description['spa']
        if not isinstance(spa, str):
            raise Exception('spa must be a string')
    if spa:
        config = [('root', description['path']), ('try_files', '$uri', spa)]
    else:
        config = [('alias', description['path'])]
    if 'index' in description:
        if spa:
            raise Exception('cannot use both spa and index options in static files')
        config.append(('index', description['index']))
    return 'location', description['location'], config


def generate_static_files(description):
    if isinstance(description, (str, dict)):
        description = [description]
    return [generate_static_files_entry(item) for item in description]


_upstream_counter = 0


def parse_single_upstream(server_name, config):
    if isinstance(config, str):
        config = {
            'url': config,
            'name': f'{server_name}-upstream',
            'location': '/'
        }
    elif 'name' not in config:
        global _upstream_counter
        _upstream_counter += 1
        config['name'] = f'upstream-auto-{_upstream_counter}'

    upstream = {
        'name': config['name'],
        'location': config['location'],
    }
    for proto in 'http', 'uwsgi':
        if config['url'].startswith(f'{proto}://'):
            upstream.update(url=config['url'][len(proto) + 3:], type=proto)
            return upstream
    raise Exception(f'Please prefix {server_name}.upstream url with protocol name')


def parse_upstreams(input_config):
    upstreams = []
    configs = defaultdict(list)
    for server_name, server_config in input_config.get('servers', {}).items():
        if 'upstream' not in server_config:
            continue

        config = server_config['upstream']
        if isinstance(config, (str, dict)):
            config = [config]
        for entry in config:
            upstream = parse_single_upstream(server_name, entry)
            upstreams.append(('upstream', upstream['name'], [
                ('server', upstream['url']),
            ]))
            configs[server_name].append(upstream)
    return upstreams, configs


def generate_tls_config(cert):
    config = [
        ('ssl_certificate', f'/etc/nginx/certs/{cert["certificate"]}'),
        ('ssl_certificate_key', f'/etc/nginx/certs/{cert["key"]}'),
    ]
    if 'root_chain' in cert:
        config.append(('ssl_trusted_certificate', f'/etc/nginx/certs/{cert["root_chain"]}'))
    return config


def activate_lets_encrypt(server_name):
    with open('/tmp/le-domain.txt', 'w') as f:
        f.write(server_name)
    return {
        'certificate': f'live/{server_name}/fullchain.pem',
        'key': f'live/{server_name}/privkey.pem',
        'root_chain': f'live/{server_name}/chain.pem',
    }


def generate_server(name, description, upstreams):
    server_name = None
    tls = None
    check_host_header = True
    extra_config = []

    for item, content in description.items():
        if item == 'server_raw_options':
            if not isinstance(content, list):
                raise Exception(f'{name}.server_raw_options must be an array')
            for option in content:
                extra_config.append(option.split(' '))
        elif item == 'server_name':
            server_name = content
        elif item == 'static_files':
            extra_config.extend(generate_static_files(content))
        elif item == 'tls':
            tls = content
        elif item == 'check_host_header':
            if not isinstance(content, bool):
                raise Exception(f'{name}.check_host_header must be a boolean value')
            check_host_header = content
        elif item == 'upstream':
            pass  # handled by parse_upstreams
        else:
            raise Exception(f'unknown option {name}.{item}')

    if not server_name:
        raise Exception('server_name is required')

    use_tls = True
    if tls is False:
        use_tls = False
    elif isinstance(tls, str) and tls == 'auto':
        cert = activate_lets_encrypt(server_name)
        extra_config.extend(generate_tls_config(cert))
        extra_config.append(('location', '/.well-known/acme-challenge', [('root', '/var/www/letsencrypt')]))
    elif isinstance(tls, dict):
        extra_config.extend(generate_tls_config(tls))
    elif isinstance(tls, list):
        for cert in tls:
            extra_config.extend(generate_tls_config(cert))
    else:
        raise Exception(f'{name}.tls value is invalid')

    for upstream in upstreams:
        config = [
            ('proxy_set_header', 'X-Request-ID', '$request_id'),
            ('proxy_set_header', 'X-Forwarded-For', '$proxy_add_x_forwarded_for'),
            ('proxy_set_header', 'Host', '$http_host'),
        ]
        if upstream['type'] == 'uwsgi':
            config.append(('include', 'uwsgi_params'))
            config.append(('uwsgi_pass', upstream['name']))
        elif upstream['type'] == 'http':
            config.append(('proxy_pass', f'http://{upstream["name"]}'))
        else:
            raise NotImplementedError(f'Someone was naughty and did not implement the {upstream["type"]} upstream type')
        extra_config.append(('location', upstream['location'], config))

    servers = []
    if use_tls:
        servers.append(('server', [
            ('#', name, '- force HTTPS'),
        ] + base_config_https_redirect(server_name, check_host_header)))
        servers.append(('server', [
            ('#', name),
        ] + base_config_https(server_name, check_host_header) + extra_config))
    else:
        servers.append(('server', [
            ('#', name),
        ] + base_config_http(server_name, check_host_header) + extra_config))
    return servers


def generate_servers(app_conf, upstreams):
    servers = []
    input_servers = app_conf.get('servers', {})
    for name, description in input_servers.items():
        servers.extend(generate_server(name, description, upstreams.get(name, [])))
    return servers


def generate_http(app_conf):
    http = _NGINX_HTTP.copy()
    for entry in app_conf.get('http_raw_options', []):
        parts = entry.split(' ')
        if parts[0] == 'include':
            http.append(parts)
            continue
        for idx, option in enumerate(http):
            if option[0] == parts[0]:
                http[idx] = parts
                break
        else:
            http.append(parts)
    return http


def main():
    try:
        with open(sys.argv[1]) as config_yml:
            app_conf = yaml.safe_load(config_yml)
    except FileNotFoundError:
        print(sys.stderr, f'This image requires {sys.argv[1]} to be present. Did you forget to docker-mount it?')
        sys.exit(1)

    upstreams_conf, upstreams_data = parse_upstreams(app_conf)
    servers_conf = generate_servers(app_conf, upstreams_data)
    http = generate_http(app_conf)
    http.extend(upstreams_conf)
    http.extend(servers_conf)
    nginx_conf = _NGINX_GLOBALS.copy()
    nginx_conf.append(('http', http))
    emit_nginx_conf(nginx_conf)


if __name__ == '__main__':
    main()
