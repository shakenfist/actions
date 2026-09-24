#!/usr/bin/env python3

"""Prove a Proxmox console .vv's proxy will open a tunnel, from here.

    proxmox-connect-probe.py <vv-file>

Reads "proxy", "host" and "tls-port" from the .vv, resolves the proxy's
host BY NAME, the way a .vv client does, and sends spiceproxy the CONNECT a
client would:

    CONNECT <host>:<tls-port> HTTP/1.1
    Host: <host>:<tls-port>

It exits 0 only if spiceproxy answers with a 200 status line, and then
closes the tunnel without sending anything down it. TLS and SPICE are the
job of the client under test, not of this probe; what it settles is that
the name resolves on this machine, the node is reachable from it, and the
ticket is accepted -- so that a failure in any of those is reported as a
substrate failure here, rather than later as a client's opaque TLS or DNS
error.

Both halves of that request are load-bearing, and neither failure says
so: spiceproxy reads the connect string from the Host header, not the
request line, and it refuses the tunnel unless the port matches the one
signed into the pseudo-hostname. Either mistake is answered "401 invalid
ticket", which reads exactly like an expired ticket.

It prints the proxy's name and address and the status line. It never prints
the pseudo-hostname in "host", which carries a live proxy ticket, nor
anything else from the .vv. The ticket is good for about 30 seconds from the
mint, so run this straight after minting.

Exit codes: 0 tunnel accepted, 1 refused or unreachable, 2 unusable .vv.
"""

import configparser
import re
import socket
import sys
import urllib.parse


DEFAULT_PROXY_PORT = 3128
TIMEOUT_SECONDS = 10
# spiceproxy answers "HTTP/1.0 200 OK" and a blank line, then relays. Its
# refusals are short too, so this is only a bound on a misbehaving peer.
MAX_RESPONSE_HEADER = 4096


class ProbeError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def read_vv(path):
    # No interpolation: the "ca" value is full of characters configparser
    # would otherwise try to interpret.
    parser = configparser.RawConfigParser(strict=False)
    try:
        with open(path) as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        # The parser quotes the offending line on some errors, and a line
        # of a .vv may be a credential, so only the error's class is shown.
        raise ProbeError('cannot read %s as a .vv file (%s)' % (path, type(e).__name__), 2)
    if not parser.has_section('virt-viewer'):
        raise ProbeError('%s has no [virt-viewer] section' % path, 2)
    section = parser['virt-viewer']
    for key in ('proxy', 'host', 'tls-port'):
        if not section.get(key):
            raise ProbeError('%s has no "%s" key' % (path, key), 2)
    return section['proxy'], section['host'], section['tls-port']


def parse_proxy(proxy):
    # Proxmox writes "http://<hostname -f>:3128". The scheme is optional in
    # the .vv format and 3128 is the default port, as remote-viewer has it.
    if '://' not in proxy:
        proxy = 'http://' + proxy
    parts = urllib.parse.urlsplit(proxy)
    if parts.scheme != 'http':
        raise ProbeError('the .vv proxy is not an http:// proxy: %s' % proxy, 2)
    try:
        port = parts.port or DEFAULT_PROXY_PORT
    except ValueError:
        raise ProbeError('the .vv proxy has an invalid port: %s' % proxy, 2)
    if not parts.hostname:
        raise ProbeError('the .vv proxy has no host: %s' % proxy, 2)
    return parts.hostname, port


def connect_target(host, tls_port):
    # The pseudo-hostname goes into a request line and a header, so refuse
    # anything that could end either. Fixed messages: this is the ticket.
    if not re.fullmatch(r'[A-Za-z0-9._:\[\]-]+', host):
        raise ProbeError('the .vv "host" is not a hostname', 2)
    if not re.fullmatch(r'[0-9]{1,5}', tls_port):
        raise ProbeError('the .vv "tls-port" is not a port number', 2)
    return '%s:%s' % (host, tls_port)


def resolve(name, port):
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ProbeError(
            'the proxy host %s does not resolve on this machine (%s). A runner '
            'reaches the node by the name in its tickets, so that name must '
            'be in /etc/hosts or DNS here.' % (name, e))
    return infos


def open_tunnel(name, port):
    last_error = None
    for family, socktype, proto, _, sockaddr in resolve(name, port):
        address = sockaddr[0]
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(TIMEOUT_SECONDS)
            sock.connect(sockaddr)
        except OSError as e:
            last_error = '%s (%s): %s' % (name, address, e)
            continue
        print('proxy %s:%d resolved to %s, connected' % (name, port, address), flush=True)
        return sock
    raise ProbeError('cannot connect to the proxy at %s' % last_error)


def read_status_line(sock):
    # A byte at a time, so that nothing past the response header is read:
    # after a 200 the next bytes would be the tunnelled stream.
    buf = b''
    while b'\r\n\r\n' not in buf and b'\n\n' not in buf:
        try:
            chunk = sock.recv(1)
        except OSError as e:
            raise ProbeError('the proxy did not answer the CONNECT: %s' % e)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_RESPONSE_HEADER:
            break
    if not buf:
        raise ProbeError('the proxy closed the connection without answering the CONNECT')
    return buf.splitlines()[0].decode('ascii', errors='replace').strip()


def probe(vv_path):
    proxy, host, tls_port = read_vv(vv_path)
    name, port = parse_proxy(proxy)
    target = connect_target(host, tls_port)

    sock = open_tunnel(name, port)
    try:
        request = 'CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (target, target)
        sock.sendall(request.encode('ascii'))
        status = read_status_line(sock)
    finally:
        sock.close()

    print('proxy %s:%d answered: %s' % (name, port, status), flush=True)
    if not re.match(r'HTTP/1\.[01] 200(\s|$)', status):
        raise ProbeError('the proxy refused the tunnel; expected a 200 status line')


def main(argv):
    if len(argv) != 2 or argv[1] in ('-h', '--help'):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        probe(argv[1])
    except ProbeError as e:
        print('proxmox-connect-probe: %s' % e, file=sys.stderr)
        return e.code
    print('proxmox-connect-probe: tunnel accepted')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
