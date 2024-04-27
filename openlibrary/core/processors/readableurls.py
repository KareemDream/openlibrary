"""Handlers for adding and editing books."""

import io
import json
import csv
import datetime

from typing import Literal, overload, NoReturn

from infogami import config
from infogami.core.db import ValidationException
from infogami.utils import delegate
from infogami.utils.view import safeint, add_flash_message
from infogami.infobase.client import ClientException

from openlibrary.plugins.worksearch.search import get_solr
from openlibrary.core.helpers import uniq
from openlibrary.i18n import gettext as _
from openlibrary import accounts
import logging

from openlibrary.plugins.upstream import spamcheck, utils
from openlibrary.plugins.upstream.models import Author, Edition, Work
from openlibrary.plugins.upstream.utils import render_template, fuzzy_find

from openlibrary.plugins.upstream.account import as_admin
from openlibrary.plugins.recaptcha import recaptcha

import urllib
from web.webapi import SeeOther

def encode_url_path(url: str) -> str:
    """Encodes the path part of the url to avoid issues with non-latin characters as
    non-latin characters was breaking `web.seeother`.

    >>> encode_url_path('/books/OL10M/Вас_ил/edit?mode=add-work')
    '/books/OL10M/%D0%92%D0%B0%D1%81_%D0%B8%D0%BB/edit?mode=add-work'
    >>> encode_url_path('')
    ''
    >>> encode_url_path('/')
    '/'
    >>> encode_url_path('/books/OL11M/进入该海域?mode=add-work')
    '/books/OL11M/%E8%BF%9B%E5%85%A5%E8%AF%A5%E6%B5%B7%E5%9F%9F?mode=add-work'
    """
    result = urllib.parse.urlparse(url)
    correct_path = "/".join(urllib.parse.quote(part) for part in result.path.split("/"))
    result = result._replace(path=correct_path)
    return result.geturl()

"""Various web.py application processors used in OL.
"""

import logging
import os
import web

from infogami.utils.view import render
from openlibrary.core import helpers as h

import urllib

logger = logging.getLogger("openlibrary.readableurls")

try:
    from booklending_utils.openlibrary import is_exclusion
except ImportError:

    def is_exclusion(obj):
        """Processor for determining whether records require exclusion"""
        return False


class ReadableUrlProcessor:
    """Open Library code works with urls like /books/OL1M and
    /books/OL1M/edit. This processor seamlessly changes the urls to
    /books/OL1M/title and /books/OL1M/title/edit.

    The changequery function is also customized to support this.
    """

    patterns = [
        (r'/\w+/OL\d+M', '/type/edition', 'title', 'untitled'),
        (r'/\w+/ia:[a-zA-Z0-9_\.-]+', '/type/edition', 'title', 'untitled'),
        (r'/\w+/OL\d+A', '/type/author', 'name', 'noname'),
        (r'/\w+/OL\d+W', '/type/work', 'title', 'untitled'),
        (r'/[/\w\-]+/OL\d+L', '/type/list', 'name', 'unnamed'),
    ]

    def __call__(self, handler):
        if web.ctx.path.startswith("/l/"):
            raise web.seeother("/languages/" + web.ctx.path[len("/l/") :])

        if web.ctx.path.startswith("/user/") and not web.ctx.site.get(web.ctx.path):
            raise web.seeother("/people/" + web.ctx.path[len("/user/") :])

        real_path, readable_path = get_readable_path(
            web.ctx.site, web.ctx.path, self.patterns, encoding=web.ctx.encoding
        )

        normalized_path = web.ctx.path.rstrip('/')

        real_path, readable_path = get_readable_path(
            web.ctx.site, normalized_path, self.patterns, encoding=web.ctx.encoding
        )

        if readable_path.endswith('/'):
            readable_path = readable_path.rstrip('/')
        readable_path = encode_url_path(readable_path)
        real_path = encode_url_path(real_path)

        print(readable_path, "====PRINTING READABLE PATH===")
        print(web.ctx.path, "====PRINTING WEB.CTX.PATH===")
        if ("json" not in readable_path):
            print("===HELLO WORLD")
            print(readable_path, "==== testing readable path====")

        if (
            readable_path != web.ctx.path
            and readable_path != urllib.parse.quote(web.safestr(web.ctx.path))
            and web.ctx.method == "GET"
        ):
            raise web.redirect(
                web.safeunicode(readable_path) + web.safeunicode(web.ctx.query)
            )

        web.ctx.readable_path = readable_path
        web.ctx.path = real_path
        web.ctx.fullpath = web.ctx.path + web.ctx.query
        out = handler()
        V2_TYPES = [
            'works',
            'books',
            'people',
            'authors',
            'publishers',
            'languages',
            'account',
        ]

        if web.ctx.get('exclude'):
            web.ctx.status = "404 Not Found"
            return render.notfound(web.ctx.path)

        return out

   


def _get_object(site, key):
    """Returns the object with the given key.

    If the key has an OLID and no object is found with that key, it tries to
    find object with the same OLID. OL database makes sures that OLIDs are
    unique.
    """
    obj = site.get(key)

    if obj is None and key.startswith("/a/"):
        key = "/authors/" + key[len("/a/") :]
        obj = key and site.get(key)

    if obj is None and key.startswith("/b/"):
        key = "/books/" + key[len("/b/") :]
        obj = key and site.get(key)

    if obj is None and key.startswith("/user/"):
        key = "/people/" + key[len("/user/") :]
        obj = key and site.get(key)

    basename = key.split("/")[-1]

    # redirect all /.*/ia:foo to /books/ia:foo
    if obj is None and basename.startswith("ia:"):
        key = "/books/" + basename
        obj = site.get(key)

    # redirect all /.*/OL123W to /works/OL123W
    if obj is None and basename.startswith("OL") and basename.endswith("W"):
        key = "/works/" + basename
        obj = site.get(key)

    # redirect all /.*/OL123M to /books/OL123M
    if obj is None and basename.startswith("OL") and basename.endswith("M"):
        key = "/books/" + basename
        obj = site.get(key)

    # redirect all /.*/OL123A to /authors/OL123A
    if obj is None and basename.startswith("OL") and basename.endswith("A"):
        key = "/authors/" + basename
        obj = site.get(key)

    # Disabled temporarily as the index is not ready the db

    # if obj is None and web.re_compile(r"/.*/OL\d+[A-Z]"):
    #    olid = web.safestr(key).split("/")[-1]
    #    key = site._request("/olid_to_key", data={"olid": olid}).key
    #    obj = key and site.get(key)
    return obj


def get_readable_path(site, path, patterns, encoding=None):
    """Returns real_path and readable_path from the given path.

    The patterns is a list of (path_regex, type, property_name, default_value)
    tuples.
    """

    def match(path):
        for pat, _type, _property, default_title in patterns:
            m = web.re_compile('^' + pat).match(path)
            if m:
                prefix = m.group()
                extra = web.lstrips(path, prefix)
                tokens = extra.split("/", 2)

                # `extra` starts with "/". So first token is always empty.
                middle = web.listget(tokens, 1, "")
                suffix = web.listget(tokens, 2, "")
                if suffix:
                    suffix = "/" + suffix

                return _type, _property, default_title, prefix, middle, suffix
        return None, None, None, None, None, None

    _type, _property, default_title, prefix, middle, suffix = match(path)

    if _type is None:
        path = web.safeunicode(path)
        return (path, path)

    if encoding is not None or path.endswith((".json", ".rdf", ".yml")):
        key, ext = os.path.splitext(path)

        thing = _get_object(site, key)
        if thing:
            path = thing.key + ext
        path = web.safeunicode(path)
        return (path, path)

    thing = _get_object(site, prefix)

    # get_object may handle redirections.
    if thing:
        prefix = thing.key

    if thing and thing.type.key == _type:
        title = thing.get(_property) or default_title
        try:
            # Explicitly only run for python3 to solve #4033
            from urllib.parse import quote_plus

            middle = '/' + quote_plus(h.urlsafe(title.strip()))
        except ImportError:
            middle = '/' + h.urlsafe(title.strip())
    else:
        middle = ""

    if is_exclusion(thing):
        web.ctx.exclude = True

    prefix = web.safeunicode(prefix)
    middle = web.safeunicode(middle)
    suffix = web.safeunicode(suffix)

    return (prefix + suffix, prefix + middle + suffix)
