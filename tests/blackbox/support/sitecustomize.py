"""Keep Python children on loopback, including their DNS lookups.

The parent pytest guard cannot reach subprocesses. Native replacement servers
use the same local-only configuration and dead proxies, without this Python hook.
"""

import socket


def _check(host):
    if host not in (None, "", "localhost", "127.0.0.1", "::1", b"localhost"):
        raise OSError("Blackbox processes may connect only to loopback")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_sendto = socket.socket.sendto
_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname
_gethostbyname_ex = socket.gethostbyname_ex


def connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(address[0])
    return _connect(sock, address)


def connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(address[0])
    return _connect_ex(sock, address)


def sendto(sock, data, *args):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _check(args[-1][0])
    return _sendto(sock, data, *args)


def getaddrinfo(host, *args, **kwargs):
    _check(host)
    return _getaddrinfo(host, *args, **kwargs)


def gethostbyname(host):
    _check(host)
    return _gethostbyname(host)


def gethostbyname_ex(host):
    _check(host)
    return _gethostbyname_ex(host)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.socket.sendto = sendto
socket.getaddrinfo = getaddrinfo
socket.gethostbyname = gethostbyname
socket.gethostbyname_ex = gethostbyname_ex
