#!/bin/python
#
# wopiserver.py
'''
The Web-application Open Platform Interface (WOPI) gateway for CERNBox

Author: Giuseppe.LoPresti@cern.ch
CERN/IT-ST
'''

import sys, os, time, socket, ConfigParser, platform
import logging.handlers, logging
import urllib, httplib, json
try:
  import flask                 # Flask app server, python-flask-0.10.1-4.el7.noarch.rpm + pyOpenSSL-0.13.1-3.el7.x86_64.rpm
  import jwt                   # PyJWT Jason Web Token, python-jwt-1.4.0-2.el7.noarch.rpm
  import xrootiface as xrdcl   # a wrapper around the xrootd python bindings, xrootd-python-4.4.x.el7.x86_64.rpm
except ImportError:
  print "Missing modules, please install xrootd-python, python-flask, python-jwt"
  sys.exit(-1)

# the following constant is replaced on the fly when generating the RPM (cf. spec file)
WOPISERVERVERSION = 'git'

# these are the xattr keys used for conflicts resolution and locking on the remote storage
kLastWopiSaveTime = 'oc.wopi.lastwritetime'
kWopiLock = 'oc.wopi.lock'

try:
  _loglevels = {"Critical": logging.CRITICAL,  # 50
                "Error":    logging.ERROR,     # 40
                "Warning":  logging.WARNING,   # 30
                "Info":     logging.INFO,      # 20
                "Debug":    logging.DEBUG      # 10
               }
  lastConfigReadTime = time.time()
  # read the configuration
  config = ConfigParser.SafeConfigParser()
  config.readfp(open('/etc/wopi/wopiserver.defaults.conf'))    # fails if the file does not exist
  config.read('/etc/wopi/wopiserver.conf')
  # prepare the Flask web app
  app = flask.Flask("WOPIServer")
  log = app.logger
  log.setLevel(_loglevels[config.get('general', 'loglevel')])
  loghandler = logging.FileHandler('/var/log/cernbox/wopiserver.log')
  loghandler.setFormatter(logging.Formatter(fmt='%(asctime)s %(name)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%dT%H:%M:%S'))
  log.addHandler(loghandler)
  wopisecret = open(config.get('security', 'secretfile')).read()
  tokenvalidity = config.getint('general', 'tokenvalidity')
  xrdcl.init(config, log)                          # initialize the xroot client module
  config.get('general', 'allowedclients')          # read this to make sure it is configured
except Exception, e:
  # any error we got here with the configuration is fatal
  log.critical('msg="Failed to read config, bailing out" error=%s' % e)
  sys.exit(-1)


def generateAccessToken(ruid, rgid, filename, canedit):
  '''Generate an access token for a given file of a given user, and returns a URL-encoded string
  suitable to be passed as a WOPISrc value to a Microsoft Office Online server.
  Access to this function is protected by source IP address.'''
  try:
    # stat now the file to check for existence and get inode and modification time
    # the inode serves as fileid, the mtime can be used for version information
    statx = xrdcl.statx(filename, ruid, rgid)
    inode = statx[2]
    mtime = statx[12]
  except IOError, e:
    log.info('msg="Requested file not found" filename="%s" error="%s"' % (filename, e))
    raise
  exptime = int(time.time()) + tokenvalidity
  acctok = jwt.encode({'ruid': ruid, 'rgid': rgid, 'filename': filename, 'canedit': canedit, 'mtime': mtime,
                       'exp': exptime}, wopisecret, algorithm='HS256')
  log.debug('msg="access_token generated" ruid="%s" rgid="%s" filename="%s" canedit="%s" mtime="%s" expiration="%d"' % \
            (ruid, rgid, filename, canedit, mtime, exptime))
  # return the inode == fileid and the access token
  return inode, acctok


def refreshConfig():
  '''re-read the configuration file every 300 secs to catch any runtime parameter change'''
  global lastConfigReadTime
  global tokenvalidity
  if time.time() > lastConfigReadTime + 300:
    lastConfigReadTime = time.time()
    config.read('/etc/wopi/wopiserver.conf')
    # refresh some general parameters
    tokenvalidity = config.getint('general', 'tokenvalidity')
    log.setLevel(_loglevels[config.get('general', 'loglevel')])


# The Web Application starts here
@app.route("/")
def index():
  '''Return a default index page with some user-friendly information about this service'''
  log.info('msg="Accessed index page" client="%s"' % flask.request.remote_addr)
  return """
    <div align="center" style="color:#000080; padding-top:50px; font-family:Verdana; size:11">
    This is the CERNBox <a href=http://wopi.readthedocs.io>WOPI</a> server for Microsoft Office Online.
    To use this service, please log in to your <a href=https://cernbox.cern.ch>CERNBox</a> account
    and click on the Open button next to your Microsoft Office documents.</div>
    <br><br><br><br><br><br><br><br><br><br><hr>
    <i>CERNBox WOPI Server %s. Powered by Flask %s for Python %s</i>.
    """ % (WOPISERVERVERSION, flask.__version__, platform.python_version())


@app.route("/wopi/cboxopen", methods=['GET'])
def wopiOpen():
  '''Get a WOPISrc target and an access token to be passed to Microsoft Office online for accessing a given file for a given user'''
  refreshConfig()
  req = flask.request
  # first resolve the client: only our OwnCloud servers shall use this API
  allowedclients = config.get('general', 'allowedclients').split()
  for c in allowedclients:
    for ip in socket.getaddrinfo(c, None):
      if ip[4][0] == req.remote_addr:
        # we got a match, generate the access token
        ruid = req.args['ruid']
        rgid = req.args['rgid']
        filename = req.args['filename']
        canedit = ('canedit' in req.args and req.args['canedit'].lower() == 'yes')
        try:
          inode, acctok = generateAccessToken(ruid, rgid, filename, canedit)
          log.info('msg="wopiOpen: access token set" host="%s" user="%s:%s" filename="%s" canedit="%r" inode="%s" token="%s"' % \
                   (req.remote_addr, ruid, rgid, filename, canedit, inode, acctok))
          # return an URL-encoded URL for the Office Online server
          return urllib.quote_plus('http://%s:8080/wopi/files/%s' % (socket.gethostname(), inode)) + \
                 '&access_token=%s' % acctok      # no need to URL-encode the JWT token
        except IOError:
          return 'File not found', httplib.NOT_FOUND
  # no match found, fail
  log.info('msg="wopiOpen: unauthorized access attempt" client="%s"' % flask.request.remote_addr)
  return 'Client IP not authorized', httplib.UNAUTHORIZED


@app.route("/wopi/files/<fileid>", methods=['GET'])
def wopiCheckFileInfo(fileid):
  '''Implements the CheckFileInfo WOPI call'''
  refreshConfig()
  try:
    acctok = jwt.decode(flask.request.args['access_token'], wopisecret, algorithms=['HS256'])
    if acctok['exp'] < time.time():
      raise jwt.exceptions.DecodeError
    log.info('msg="CheckFileInfo" user="%s:%s" filename"%s" fileid="%s"' % \
             (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
    statInfo = xrdcl.statx(acctok['filename'], acctok['ruid'], acctok['rgid'])
    # populate metadata for this file
    filemd = {}
    filemd['BaseFileName'] = os.path.basename(acctok['filename'])
    filemd['OwnerId'] = statInfo[5] + ':' + statInfo[6]
    filemd['UserId'] = acctok['ruid'] + ':' + acctok['rgid']
    filemd['Size'] = statInfo[8]
    filemd['Version'] = statInfo[12]
    filemd['SupportsUpdate'] = filemd['UserCanWrite'] = filemd['SupportsLocks'] = acctok['canedit']
    filemd['SupportsRename'] = filemd['UserCanRename'] = False
    filemd['UserCanNotWriteRelative'] = True     # XXX for the time being, until the PutRelative function is implemented
    filemd['DownloadUrl'] = config.get('general', 'downloadurl') + \
                            '?dir=' + urllib.quote_plus(os.path.dirname(acctok['filename'])) + \
                            '&files=' + urllib.quote_plus(filemd['BaseFileName'])
    # send it in JSON format
    resp = flask.Response(json.dumps(filemd), mimetype='application/json')
    return resp
  except (jwt.exceptions.DecodeError, jwt.exceptions.ExpiredSignatureError) as e:
    log.warning('msg="Signature verification failed" token="%s"' % flask.request.args['access_token'])
    return 'Invalid access token', httplib.UNAUTHORIZED
  except IOError, e:
    log.info('msg="Requested file not found" filename="%s" error="%s"' % (acctok['filename'], e))
    return 'File not found', httplib.NOT_FOUND
  except KeyError, e:
    log.error('msg="Invalid access token, missing %s field"' % e)
    return 'Invalid access token', httplib.UNAUTHORIZED
  except Exception, e:
    log.error('msg="Unexpected exception caught" exception="%s"' % e)
    log.debug(sys.exc_info())
    return 'Internal error', httplib.INTERNAL_SERVER_ERROR


@app.route("/wopi/files/<fileid>/contents", methods=['GET'])
def wopiGetFile(fileid):
  '''Implements the GetFile WOPI call'''
  refreshConfig()
  try:
    acctok = jwt.decode(flask.request.args['access_token'], wopisecret, algorithms=['HS256'])
    if acctok['exp'] < time.time():
      raise jwt.exceptions.DecodeError
    log.info('msg="GetFile" user="%s:%s" filename="%s" fileid="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
    # stream file from storage to client
    resp = flask.Response(xrdcl.readfile(acctok['filename'], acctok['ruid'], acctok['rgid']), mimetype='application/octet-stream')
    resp.headers['X-WOPI-ItemVersion'] = acctok['mtime']
    return resp
  except (jwt.exceptions.DecodeError, jwt.exceptions.ExpiredSignatureError) as e:
    log.warning('msg="Signature verification failed" token="%s"' % flask.request.args['access_token'])
    return 'Invalid access token', httplib.UNAUTHORIZED
  except Exception, e:
    log.error('msg="Unexpected exception caught" exception="%s"' % e)
    log.debug(sys.exc_info())
    return 'Internal error', httplib.INTERNAL_SERVER_ERROR


# the following operations are all called on POST /wopi/files/<fileid>

def wopiLock(fileid, reqheaders, acctok):
  '''Implements the Lock and RefreshLock WOPI calls'''
  lock = reqheaders['X-WOPI-Lock']
  log.info('msg="%s" user="%s:%s" filename="%s" fileid="%s" lock="%s"' % \
           (reqheaders['X-WOPI-Override'].title(), acctok['ruid'], acctok['rgid'], acctok['filename'], fileid, lock))
  if reqheaders['X-WOPI-Override'] == 'LOCK':
    # on first lock, set an xattr with the current time for later conflicts checking
    try:
      xrdcl.setxattr(acctok['filename'], acctok['ruid'], acctok['rgid'], kLastWopiSaveTime, int(time.time()))
    except IOError:
      # not fatal, but will generate a conflict file later on, so log a warning
      log.warning('msg="Unable to set xattr" user="%s:%s" filename="%s" fileid="%s"' % \
                  (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
  return 'OK', httplib.OK

def wopiUnlock(fileid, reqheaders, acctok):
  '''Implements the Unlock WOPI call'''
  lock = reqheaders['X-WOPI-Lock']
  log.info('msg="Unlock" user="%s:%s" filename="%s" fileid="%s" lock="%s"' % \
           (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid, lock))
  # remove any extended attribute related to conflicts handling
  try:
    xrdcl.rmxattr(acctok['filename'], acctok['ruid'], acctok['rgid'], kLastWopiSaveTime)
  except IOError:
    # ignore, it's not worth to report anything here
    pass
  return 'OK', httplib.OK

def wopiGetLock(fileid, reqheaders_unused, acctok):
  '''Implements the GetLock WOPI call'''
  log.info('msg="GetLock" user="%s:%s" filename="%s" fileid="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
  return 'Not supported', httplib.NOT_IMPLEMENTED

def wopiPutRelative(fileid, reqheaders_unused, acctok):
  '''Implements the PutRelative WOPI call'''
  # http://wopi.readthedocs.io/projects/wopirest/en/latest/files/PutRelativeFile.html
  log.info('msg="PutRelative" user="%s:%s" filename="%s" fileid="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
  return 'Not supported', httplib.NOT_IMPLEMENTED

def wopiDeleteFile(fileid, reqheaders_unused, acctok):
  '''Implements the DeleteFile WOPI call'''
  log.info('msg="DeleteFile" user="%s:%s" filename="%s" fileid="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
  try:
    xrdcl.removefile(acctok['filename'], acctok['ruid'], acctok['rgid'])
    return 'OK', httplib.OK
  except IOError:
    return 'Internal error', httplib.INTERNAL_SERVER_ERROR

def wopiRenameFile(fileid, reqheaders_unused, acctok):
  '''Implements the RenameFile WOPI call'''
  log.info('msg="RenameFile" user="%s:%s" filename="%s" fileid="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
  #targetname = reqheaders['X-WOPI-RequestedName']
  return 'Not supported', httplib.NOT_IMPLEMENTED

@app.route("/wopi/files/<fileid>", methods=['POST'])
def wopiPost(fileid):
  '''A dispatcher metod for all files POST operations'''
  refreshConfig()
  try:
    acctok = jwt.decode(flask.request.args['access_token'], wopisecret, algorithms=['HS256'])
    if acctok['exp'] < time.time():
      raise jwt.exceptions.DecodeError
    headers = flask.request.headers
    op = headers['X-WOPI-Override']       # must be one of the following strings, throws KeyError if missing
    if op in ('LOCK', 'REFRESH_LOCK'):
      return wopiLock(fileid, headers, acctok)
    elif op == 'UNLOCK':
      return wopiUnlock(fileid, headers, acctok)
    elif op == 'GET_LOCK':
      return wopiGetLock(fileid, headers, acctok)
    elif op == 'PUT_RELATIVE':
      return wopiPutRelative(fileid, headers, acctok)
    elif op == 'DELETE_FILE':
      return wopiDeleteFile(fileid, headers, acctok)
    elif op == 'RENAME_FILE':
      return wopiRenameFile(fileid, headers, acctok)
    else:
      return 'Unknown operation %s found in header' % op, httplib.BAD_REQUEST
  except (jwt.exceptions.DecodeError, jwt.exceptions.ExpiredSignatureError) as e:
    log.warning('msg="Signature verification failed" token="%s"' % flask.request.args['access_token'])
    return 'Invalid access token', httplib.UNAUTHORIZED
  except KeyError, e:
    return 'Missing header %s in POST request' % e, httplib.BAD_REQUEST
  except Exception, e:
    log.error('msg="Unexpected exception caught" exception="%s"' % e)
    log.debug(sys.exc_info())
    return 'Internal error', httplib.INTERNAL_SERVER_ERROR


@app.route("/wopi/files/<fileid>/contents", methods=['POST'])
def wopiPutFile(fileid):
  '''Implements the PutFile WOPI call'''
  refreshConfig()
  try:
    acctok = jwt.decode(flask.request.args['access_token'], wopisecret, algorithms=['HS256'])
    if acctok['exp'] < time.time():
      raise jwt.exceptions.DecodeError
    log.info('msg="PostContent" user="%s:%s" filename="%s" fileid="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename'], fileid))
    # check now the destination file against conflicts
    try:
      savetime = int(xrdcl.getxattr(acctok['filename'], acctok['ruid'], acctok['rgid'], kLastWopiSaveTime))
      # OK, we got our xattr, therefore assume nobody overwrote the file
      log.debug('msg="Got lastWopiSaveTime" user="%s:%s" filename="%s" savetime="%ld"' % \
                (acctok['ruid'], acctok['rgid'], acctok['filename'], savetime))
    except IOError:
      # either the file was deleted or it was overwritten by others: force conflict
      newname, ext = os.path.splitext(acctok['filename'])
      newname = '%s_conflict-%s%s' % (newname, time.strftime('%Y%m%d-%H%M%S'), ext.strip())   # this is the OwnCloud format
      xrdcl.writefile(newname, acctok['ruid'], acctok['rgid'], flask.request.get_data())
      log.info('msg="Conflicting copy created" user="%s:%s" filename="%s"' % (acctok['ruid'], acctok['rgid'], newname))
      resp = flask.Response()
      resp.headers['X-WOPI-ServerError'] = 'Conflicting copy created'
      resp.status_code = httplib.INTERNAL_SERVER_ERROR      # return a failure so that the user can check
      return resp
    # Go for overwriting the file. Note that the entire check+write operation should be atomic,
    # but the previous check still gives the opportunity of a race condition. We just live with it
    # as OwnCloud does not seem to provide anything better...
    # Anyhow, previous versions are all stored and recoverable by the user.
    xrdcl.writefile(acctok['filename'], acctok['ruid'], acctok['rgid'], flask.request.get_data())
    log.info('msg="File successfully written" user="%s:%s" filename="%s"' % (acctok['ruid'], acctok['rgid'], acctok['filename']))
    # and retrieve again the modification time to update our xattr
    xrdcl.setxattr(acctok['filename'], acctok['ruid'], acctok['rgid'], kLastWopiSaveTime, int(time.time()))
    return 'OK', httplib.OK
  except (jwt.exceptions.DecodeError, jwt.exceptions.ExpiredSignatureError) as e:
    log.warning('msg="Signature verification failed" token="%s"' % flask.request.args['access_token'])
    return 'Invalid access token', httplib.UNAUTHORIZED
  except IOError, e:
    log.info('msg="Error writing file" filename="%s" error="%s"' % (acctok['filename'], e))
    return 'I/O Error', httplib.INTERNAL_SERVER_ERROR
  except Exception, e:
    log.error('msg="Unexpected exception caught" exception="%s"' % e)
    log.debug(sys.exc_info())
    return 'Internal error', httplib.INTERNAL_SERVER_ERROR


app.run(host='0.0.0.0', port=8080, threaded=True, debug=(config.get('general', 'loglevel') == 'Debug'))
# XXX todo: enable https and then add:    ssl_context=(config.get('security', 'wopicert'), config.get('security', 'wopikey')))
