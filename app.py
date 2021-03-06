# -*- coding: utf-8 -*-

import collections
import flask
import mwapi
import mwoauth
import os
import json
import random
import requests
import requests_oauthlib
import string
import toolforge
import yaml
from SPARQLWrapper import SPARQLWrapper, JSON

app = flask.Flask(__name__)

app.before_request(toolforge.redirect_to_https)

toolforge.set_user_agent('wikidata-senses', email='alicia@fagerving.se')
user_agent = requests.utils.default_user_agent()
sparql = SPARQLWrapper("https://query.wikidata.org/sparql", agent=user_agent)
sparql.setReturnFormat(JSON)

__dir__ = os.path.dirname(__file__)
try:
    with open(os.path.join(__dir__, 'config.yaml')) as config_file:
        app.config.update(yaml.safe_load(config_file))
except FileNotFoundError:
    print('config.yaml file not found, assuming local development setup')
    app.secret_key = ''.join(random.choice(
        string.ascii_letters + string.digits) for _ in range(64))

if 'oauth' in app.config:
    consumer_token = mwoauth.ConsumerToken(
        app.config['oauth']['consumer_key'],
        app.config['oauth']['consumer_secret'])


@app.before_request
def fixSessionToken():
    """Fix the session token after its path was changed.

    Old versions of this tool on Toolforge used '/' for the session
    cookie path, which was insecure, sending our session cookie to
    other tools as well. However, changing it to the tool name does
    not invalidate the old cookie, so the first time a client visits
    the tool again after this change was made, when we try to update
    the cookie in our response, we’re actually setting a new one with
    a different path, and on the next request we’ll receive two
    session cookies, for the old and new path. That is the earliest
    time when we can detect the situation, and deal with it by
    instructing the client to delete the '/' version and then reload.
    (We could try to decode the old session and salvage parts of it,
    but this tool only uses the session for the CSRF token and OAuth
    tokens, and salvaging either of those is probably a bad idea.)
    """

    if app.config.get('APPLICATION_ROOT', '/') == '/':
        return
    cookies_header = flask.request.headers.get('Cookie')
    if not cookies_header:
        return
    first_session = cookies_header.find('session=')
    if first_session < 0:
        return
    second_session = cookies_header[first_session+1:].find('session=')
    if second_session < 0:
        return

    response = flask.redirect(current_url())
    response.set_cookie('session', '', expires=0, path='/')
    return response


@app.template_global()
def csrf_token():
    if 'csrf_token' not in flask.session:
        flask.session['csrf_token'] = ''.join(random.choice(
            string.ascii_letters + string.digits) for _ in range(64))
    return flask.session['csrf_token']


@app.template_global()
def form_value(name):
    if 'repeat_form' in flask.g and name in flask.request.form:
        return (flask.Markup(r' value="') +
                flask.Markup.escape(flask.request.form[name]) +
                flask.Markup(r'" '))
    else:
        return flask.Markup()


@app.template_global()
def form_attributes(name):
    return (flask.Markup(r' id="') +
            flask.Markup.escape(name) +
            flask.Markup(r'" name="') +
            flask.Markup.escape(name) +
            flask.Markup(r'" ') +
            form_value(name))


@app.template_filter()
def user_link(user_name):
    return (flask.Markup(r'<a href="https://www.wikidata.org/wiki/User:') +
            flask.Markup.escape(user_name.replace(' ', '_')) +
            flask.Markup(r'">') +
            flask.Markup(r'<bdi>') +
            flask.Markup.escape(user_name) +
            flask.Markup(r'</bdi>') +
            flask.Markup(r'</a>'))


@app.template_global()
def logged_in_user_name():
    if 'user_name' in flask.g:
        return flask.g.user_name

    if 'oauth' not in app.config:
        return flask.g.setdefault('user_name', None)
    if 'oauth_access_token' not in flask.session:
        return flask.g.setdefault('user_name', None)

    access_token = mwoauth.AccessToken(**flask.session['oauth_access_token'])
    identity = mwoauth.identify('https://www.wikidata.org/w/index.php',
                                consumer_token,
                                access_token)
    return flask.g.setdefault('user_name', identity['username'])


@app.template_global()
def authentication_area():
    if 'oauth' not in app.config:
        return flask.Markup()

    user_name = logged_in_user_name()

    if user_name is None:
        return (flask.Markup(r'<a id="login" class="navbar-text" href="') +
                flask.Markup.escape(flask.url_for('login')) +
                flask.Markup(r'">Log in</a>'))

    return (flask.Markup(r'<span class="navbar-text">Logged in as ') +
            user_link(user_name) +
            flask.Markup(r'</span>'))


def get_all_languages():
    langs = collections.OrderedDict()
    sparql.setQuery("""SELECT ?number_of_lexemes ?language ?languageLabel ?languageCode
    WITH {SELECT ?language (COUNT(?l) AS ?number_of_lexemes) WHERE {
    ?l a ontolex:LexicalEntry ;
    dct:language ?language ;
    FILTER NOT EXISTS {?l ontolex:sense ?sense }
    }
    GROUP BY ?language }
    AS %languages
    WHERE {
      INCLUDE %languages
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
      ?language wdt:P424 ?languageCode.
    }
    ORDER BY DESC(?number_of_lexemes)
    LIMIT 50
      """)
    results = sparql.query().convert()
    for el in results["results"]["bindings"]:
        sense_dict = {"total": el["number_of_lexemes"]["value"],
                      "code": el["languageCode"]["value"]}
        langs[el["languageLabel"]["value"]] = sense_dict
    return langs


@app.route('/')
def index():
    all_languages = get_all_languages()
    return flask.render_template('index.html', languages=all_languages)


def get_word_data(word_id):
    api_url = ("https://www.wikidata.org/w/api.php" +
               "?action=wbgetentities&ids={}&format=json")
    word_data = requests.get(api_url.format(word_id))
    return json.loads(word_data.text)


def build_senses(form_data, lang):
    submitted_sense = form_data["sense"]
    sense_data = {"senses": [{"add": "", "glosses": {
        lang: {"language": lang, "value": submitted_sense}}}]}
    return sense_data


def full_url(endpoint, _external=True, **kwargs):
    if _external:
        return flask.url_for(
            endpoint,
            _external=True,
            _scheme=flask.request.headers.get('X-Forwarded-Proto', 'http'),
            **kwargs
        )
    else:
        return flask.url_for(
            endpoint,
            **kwargs
        )


@app.template_global()
def current_url(external=True):
    return full_url(
        flask.request.endpoint,
        _external=external,
        **flask.request.view_args
    )


def generate_auth():
    access_token = mwoauth.AccessToken(**flask.session['oauth_access_token'])
    return requests_oauthlib.OAuth1(
        client_key=consumer_token.key,
        client_secret=consumer_token.secret,
        resource_owner_key=access_token.key,
        resource_owner_secret=access_token.secret,
    )


def submit_lexeme(word_id, senses, lang):
    host = 'https://www.wikidata.org'
    session = mwapi.Session(
        host=host,
        auth=generate_auth(),
        user_agent=user_agent,
    )
    summary = "Added sense: {}.".format(
        senses["senses"][0]["glosses"][lang]["value"])
    token = session.get(action='query', meta='tokens')[
        'query']['tokens']['csrftoken']
    session.post(
        action='wbeditentity',
        data=json.dumps(senses),
        summary=summary,
        token=token,
        id=word_id
    )


def get_with_missing_senses(lang):
    sparql.setQuery("""#Lemmas with no senses
        SELECT ?l ?lemma ?posLabel WHERE {
           ?l a ontolex:LexicalEntry ; dct:language ?language ;
                wikibase:lemma ?lemma .
          ?language wdt:P424 '%s'.
              OPTIONAL {
          ?l wikibase:lexicalCategory ?pos .
                SERVICE wikibase:label
                { bd:serviceParam wikibase:language "en" . }
        }
          FILTER NOT EXISTS {?l ontolex:sense ?sense }
        }

        ORDER BY ?lemma""" % lang)
    results = sparql.query().convert()
    return results["results"]["bindings"]


def get_with_missing_senses_by_user(user_name):
    sparql.setQuery("""
        SELECT ?lexeme ?languageCode ?lexicalCategoryLabel (GROUP_CONCAT(DISTINCT ?lemma; separator = "/") AS ?lemmas) WHERE {
          hint:Query hint:optimizer "None".
          SERVICE wikibase:mwapi {
            bd:serviceParam wikibase:endpoint "www.wikidata.org";
                            wikibase:api "Generator";
                            mwapi:generator "allrevisions";
                            mwapi:garvuser "%s";
                            mwapi:garvnamespace "146";
                            mwapi:garvend "2018-05-23T00:00:00.000Z";
                            mwapi:garvlimit "max".
            ?title wikibase:apiOutput mwapi:title.
          }
          BIND(URI(CONCAT(STR(wd:), STRAFTER(?title, "Lexeme:"))) AS ?lexeme)
          MINUS { ?lexeme ontolex:sense ?sense. }
          ?lexeme wikibase:lemma ?lemma;
                  dct:language/wdt:P424 ?languageCode;
                  wikibase:lexicalCategory ?lexicalCategory.
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
        }
        GROUP BY ?lexeme ?languageCode ?lexicalCategoryLabel
        """ % user_name.replace(' ', '_').replace('\\', '\\\\').replace('"', r'\"'))
    results = sparql.query().convert()
    return results["results"]["bindings"]


def submit_sense_from_request():
    token = flask.session.pop('csrf_token', None)
    if (not token or
        token != flask.request.form.get('csrf_token') or
        not flask.request.referrer.startswith(full_url('index'))):
        flask.g.csrf_error = True
        flask.g.repeat_form = True
        return None

    if 'oauth' in app.config:
        form_data = flask.request.form
        lang = form_data['lang']
        senses = build_senses(form_data, lang)
        word_id = form_data["word_id"]
        submit_lexeme(word_id, senses, lang)
        return None
    else:
        return flask.jsonify(senses)


def show_lemma_page(lemma, lang, word_id, pos):
    return flask.render_template('lemma.html',
                                 lemma=lemma,
                                 lang=lang,
                                 word_id=word_id,
                                 pos=pos,
                                 csrf_error=flask.g.get('csrf_error', False))


@app.route('/add/<lang>', methods=['GET', 'POST'])
def add(lang):
    if flask.request.method == 'POST':
        response = submit_sense_from_request()
        if response:
            return response
        else:
            return flask.redirect(flask.url_for('add', lang=lang))

    words = get_with_missing_senses(lang)
    random_word = random.choice(words)
    random_word_id = random_word["l"]["value"].split("/")[-1]
    return show_lemma_page(lemma=random_word["lemma"]["value"],
                           lang=lang,
                           word_id=random_word_id,
                           pos=random_word["posLabel"]["value"])


@app.route('/user/<user_name>', methods=['GET', 'POST'])
def user(user_name):
    if flask.request.method == 'POST':
        response = submit_sense_from_request()
        if response:
            return response
        else:
            return flask.redirect(flask.url_for('user', user_name=user_name))

    words = get_with_missing_senses_by_user(user_name)
    random_word = random.choice(words)
    random_word_id = random_word["lexeme"]["value"].split("/")[-1]
    return show_lemma_page(lemma=random_word["lemmas"]["value"],
                           lang=random_word["languageCode"]["value"],
                           word_id=random_word_id,
                           pos=random_word["lexicalCategoryLabel"]["value"])


@app.route('/login')
def login():
    redirect, request_token = mwoauth.initiate(
        'https://www.wikidata.org/w/index.php',
        consumer_token, user_agent=user_agent)
    flask.session['oauth_request_token'] = dict(
        zip(request_token._fields, request_token))
    return flask.redirect(redirect)


@app.route('/oauth/callback')
def oauth_callback():
    request_token = mwoauth.RequestToken(
        **flask.session['oauth_request_token'])
    access_token = mwoauth.complete('https://www.wikidata.org/w/index.php',
                                    consumer_token,
                                    request_token,
                                    flask.request.query_string,
                                    user_agent=user_agent)
    flask.session['oauth_access_token'] = dict(
        zip(access_token._fields, access_token))
    return flask.redirect(flask.url_for('index'))

@app.after_request
def denyFrame(response):
    """Disallow embedding the tool’s pages in other websites.

    If other websites can embed this tool’s pages, e. g. in <iframe>s,
    other tools hosted on tools.wmflabs.org can send arbitrary web
    requests from this tool’s context, bypassing the referrer-based
    CSRF protection.
    """
    response.headers['X-Frame-Options'] = 'deny'
    return response
