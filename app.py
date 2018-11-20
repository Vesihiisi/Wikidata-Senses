# -*- coding: utf-8 -*-

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

sparql = SPARQLWrapper("https://query.wikidata.org/sparql")
app = flask.Flask(__name__)

app.before_request(toolforge.redirect_to_https)

toolforge.set_user_agent('wikidata-senses', email='alicia@fagerving.se')
user_agent = requests.utils.default_user_agent()

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
def authentication_area():
    if 'oauth' not in app.config:
        return flask.Markup()

    if 'oauth_access_token' not in flask.session:
        return (flask.Markup(r'<a id="login" class="navbar-text" href="') +
                flask.Markup.escape(flask.url_for('login')) +
                flask.Markup(r'">Log in</a>'))

    access_token = mwoauth.AccessToken(**flask.session['oauth_access_token'])
    identity = mwoauth.identify('https://www.wikidata.org/w/index.php',
                                consumer_token,
                                access_token)

    return (flask.Markup(r'<span class="navbar-text">Logged in as ') +
            user_link(identity['username']) +
            flask.Markup(r'</span>'))


def get_all_languages():
    langs = {}
    sparql = SPARQLWrapper("https://query.wikidata.org/sparql")
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
    sparql.setReturnFormat(JSON)
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


def current_url():
    return flask.url_for(
        flask.request.endpoint,
        _external=True,
        _scheme=flask.request.headers.get('X-Forwarded-Proto', 'http'),
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
    return flask.redirect(flask.url_for('add', lang=lang))


def get_with_missing_senses(lang):
    sparql = SPARQLWrapper("https://query.wikidata.org/sparql")
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
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()
    return results["results"]["bindings"]


@app.route('/add/<lang>', methods=['GET', 'POST'])
def add(lang):
    words = get_with_missing_senses(lang)
    random_word = random.choice(words)
    random_word_id = random_word["l"]["value"].split("/")[-1]
    csrf_error = False
    if flask.request.method == 'POST':
        token = flask.session.pop('csrf_token', None)
        if token and token == flask.request.form.get('csrf_token'):
            flask.session['sense'] = flask.request.form.get(
                'sense', 'sense missing')
        else:
            csrf_error = True
            flask.g.repeat_form = True

        if 'oauth' in app.config:
            form_data = flask.request.form
            senses = build_senses(form_data, lang)
            word_id = form_data["word_id"]
            return submit_lexeme(word_id, senses, lang)
        else:
            return flask.Response(json.dumps(senses),
                                  mimetype='application/json')

    return flask.render_template('lemma.html',
                                 lemma=random_word["lemma"]["value"],
                                 lang=lang,
                                 total=len(words),
                                 word_id=random_word_id,
                                 pos=random_word["posLabel"]["value"],
                                 csrf_error=csrf_error)


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
