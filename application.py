#
# BulkDM
#
# Copyright (c) 2010 by Shigeru KANEMOTO
#

# TODO:
# - Check periodically to remove unused sessions.
# - Circumvent the timeout when sending DM looping over the destinations.
# - Show DM result, not just remove from the destination list.
# - Show user profiles.

from flask import Flask
app = Flask(__name__)
app.debug = True
app.secret_key = ''			# will be overridden

from flask import (
  redirect, url_for, request, render_template,
  abort, flash, get_flashed_messages, session
)

import tweepy
from google.appengine.ext import db
import urllib
from django.utils import simplejson as json

TWEETVITE_EVENT_URL_PREFIX = 'http://tweetvite.com/event/'
TWEETVITE_GUEST_LIST_URL = 'http://tweetvite.com/api/1.0/rest/events/guest_list?public_id=%s'

################################################################################

class Config(db.Model):
  # key_name
  value = db.StringProperty()

class Session(db.Model):
  lastused = db.DateTimeProperty(required=True, auto_now=True)
  dests = db.StringListProperty(required=True)
  access_token_key = db.StringProperty()
  access_token_secret = db.StringProperty()

def session_entity():
  if session.has_key('key'):
    try:
      entity = Session.get(session['key'])
      if entity:
	return entity
    except:
      pass
  entity = Session()
  entity.put()
  session['key'] = entity.key()
  session.permanent = True
  return entity

################################################################################

#
# Configuration
#
@app.route('/config', methods=['GET', 'POST'])
def config():
  if Config.get_by_key_name('configured'):
    abort(404)
  if request.method == 'GET':
    return render_template('config.html')

  import os, base64
  Config(
    key_name='application_secret',
    value=base64.b64encode(os.urandom(24))
  ).put()

  Config(
    key_name='consumer_key',
    value=request.form.get('consumer_key', '').strip()
  ).put()

  Config(
    key_name='consumer_secret',
    value=request.form.get('consumer_secret', '').strip()
  ).put()

  Config(key_name='configured', value='yes').put()

  load_config()
  return redirect(url_for('dests'))

def load_config():
  def get(key):
    o = Config.get_by_key_name(key)
    return str(o.value) if o else ''	# DB value is unicode.

  global consumer_key, consumer_secret
  app.secret_key = get('application_secret')
  consumer_key = get('consumer_key')
  consumer_secret = get('consumer_secret')

################################################################################

#
# Handy function to get the Tweepy API object.
#
def tweepy_api(entity):
  if not entity.access_token_key:
    return False
  if not entity.access_token_secret:
    return False
  try:
    auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
    auth.set_access_token(entity.access_token_key, entity.access_token_secret)
    api = tweepy.API(auth)
    return api if api.verify_credentials() else False
  except tweepy.TweepError:
    return False

#
# Login page
#
@app.route('/login')
def login():
  ent = session_entity()
  return_url = request.args.get('return_url') or url_for('dests')
  if tweepy_api(ent):
    return redirect(return_url)
  session['return_url'] = return_url

  # OAuth
  try:
    auth = tweepy.OAuthHandler(
      consumer_key, consumer_secret,
      url_for('callback', _external=True)
    )
    authurl = auth.get_authorization_url()
    session['request_key'] = auth.request_token.key
    session['request_secret'] = auth.request_token.secret
  except tweepy.TweepError, e:
    flash(e)
    return render_template('login.html')

  ent.put()	# update the timestamp
  return render_template('login.html', authurl=authurl)

#
# The callback page for the OAuth authorization.
#
@app.route('/login/callback')
def callback():
  ent = session_entity()
  return_url = session.pop('return_url', None)

  while True:
    oauth_token = request.args.get('oauth_token')
    oauth_verifier = request.args.get('oauth_verifier')
    if not oauth_token:
      flash('Invalid callback URL')
      break

    request_token_key = session.pop('request_key', None)
    request_token_secret = session.pop('request_secret', None)
    if not request_token_key or not request_token_secret:
      flash('No request token')
      break

    if oauth_token != request_token_key:
      flash('Invalid oauth_token')
      break

    try:
      auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
      auth.set_request_token(request_token_key, request_token_secret)
      auth.get_access_token(oauth_verifier)
    except tweepy.TweepError, e:
      flash(e)
      break

    ent.access_token_key = auth.access_token.key
    ent.access_token_secret = auth.access_token.secret
    ent.put()
    session['username'] = auth.get_username()
    return redirect(return_url)

  ent.put()
  return render_template('callback.html', return_url=return_url)

#
# Logout and delete all information.
#
@app.route('/login/logout')
def logout():
  ent = session_entity()
  if ent:
    ent.delete()
  session.pop('username', None)
  session.pop('return_url', None)
  session.pop('request_key', None)
  session.pop('request_secret', None)
  return redirect(url_for('dests'))

################################################################################

#
# Get the guest list from TweetVite
#
def tweetvite_guest_list(eventid, yes=True, maybe=False, no=False):
  rsvp = []
  if yes:
    rsvp.append('Y')
  if maybe:
    rsvp.append('M')
  if no:
    rsvp.append('N')

  url = TWEETVITE_GUEST_LIST_URL % eventid
  data = json.load(urllib.urlopen(url))

  if not data.has_key('total_guests'):
    return []
  num_pages = (int(data['total_guests']) + data['count'] - 1) / data['count']

  guests = [
    guest['profile']['display_name']
    for guest in data['guests']
    if guest['rsvp'] in rsvp
  ]

  for page in range(1, num_pages):
    data = json.load(urllib.urlopen(url + '&page=%d' % page))
    guests += [
      guest['profile']['display_name']
      for guest in data['guests']
      if guest['rsvp'] in rsvp
    ]

  return guests

#
# Build the destination list
#
@app.route('/', methods=['GET', 'POST'])
def dests():
  ent = session_entity()

  if request.method == 'POST':
    if request.form.has_key('clear'):
      ent.dests = []

    if request.form.has_key('dests_add'):
      ent.dests.extend(request.form.get('dests_list', '').split())

    if request.form.has_key('delete'):
      for dest in request.form.getlist('delete'):
	try:
	  ent.dests.remove(dest)
	except KeyError:
	  pass

    if request.form.has_key('tweetvite_load'):
      eventid = request.form.get('tweetvite_id', '').strip()
      if eventid.startswith(TWEETVITE_EVENT_URL_PREFIX):
	eventid = eventid[len(TWEETVITE_EVENT_URL_PREFIX):]
      yes = request.form.get('tweetvite_yes', '') == 'on'
      maybe = request.form.get('tweetvite_maybe', '') == 'on'
      no = request.form.get('tweetvite_no', '') == 'on'
      if eventid:
	ent.dests.extend(tweetvite_guest_list(eventid, yes, maybe, no))

    ent.dests = list(set(ent.dests))
    ent.put()
    return redirect(url_for('dests'))

  ent.put()
  dests = sorted(ent.dests)
  return render_template('dests.html', dests=dests)

#
# Send the direct message.
#
@app.route('/message', methods=['GET', 'POST'])
def message():
  ent = session_entity()

  api = tweepy_api(ent)
  if not api:
    return redirect(url_for('login', return_url=url_for('message')))

  if request.method == 'POST':
    message = request.form.get('message', None)
    session['message'] = message
    if message:
      for dest in ent.dests:
	try:
	  api.send_direct_message(screen_name=dest, text=message)
	  ent.dests.remove(dest)
	except tweepy.TweepError, e:
	  flash('@%s: %s' % (dest, e))
    ent.put()
    return redirect(url_for('message'))

  ent.put()
  return render_template('message.html', dests=ent.dests)

load_config()

if __name__ == '__main__':
  app.run()
