'''
xrootiface.py

eos-xrootd interface for the IOP WOPI server

Author: Giuseppe.LoPresti@cern.ch, CERN/IT-ST
Contributions: Michael.DSilva@aarnet.edu.au
'''

import time
import os
from stat import S_ISDIR
from XRootD import client as XrdClient
from XRootD.client.flags import OpenFlags, QueryCode, MkDirFlags, StatInfoFlags

EOSVERSIONPREFIX = '.sys.v#.'

# module-wide state
config = None
log = None
xrdfs = {}    # this is to map each endpoint [string] to its XrdClient
defaultstorage = None
homepath = None


def _getxrdfor(endpoint):
  '''Look up the xrootd client for the given endpoint, create it if missing.
     Supports "default" for the defaultstorage endpoint.'''
  global xrdfs           # pylint: disable=global-statement
  global defaultstorage  # pylint: disable=global-statement
  if endpoint == 'default':
    return xrdfs[defaultstorage]
  try:
    return xrdfs[endpoint]
  except KeyError:
    # not found, create it
    xrdfs[endpoint] = XrdClient.FileSystem(endpoint)
    return xrdfs[endpoint]


def _geturlfor(endpoint):
  '''Look up the URL for a given endpoint: "default" corresponds to the defaultstorage one'''
  if endpoint == 'default':
    return defaultstorage
  return endpoint


def _eosargs(userid, atomicwrite=0, bookingsize=0):
  '''Assume userid is in the form uid:gid and split userid into uid,gid and generate extra EOS-specific arguments for the xroot URL'''
  try:
    # try to assert that userid must follow a '%d:%d' format
    userid = userid.split(':')
    if len(userid) != 2:
      raise ValueError
    ruid = int(userid[0])
    rgid = int(userid[1])
    return '?eos.ruid=%d&eos.rgid=%d' % (ruid, rgid) + '&eos.app=' + ('fuse::wopi' if not atomicwrite else 'wopi') + \
            (('&eos.bookingsize='+str(bookingsize)) if bookingsize else '')
  except (ValueError, IndexError):
    raise ValueError('Only Unix-based userid is supported with xrootd storage')


def _xrootcmd(endpoint, cmd, subcmd, userid, args):
  '''Perform the <cmd>/<subcmd> action on the special /proc/user path on behalf of the given userid.
     Note that this is entirely EOS-specific.'''
  with XrdClient.File() as f:
    url = _geturlfor(endpoint) + '//proc/user/' + _eosargs(userid) + '&mgm.cmd=' + cmd + \
          ('&mgm.subcmd=' + subcmd if subcmd else '') + '&' + args
    tstart = time.time()
    rc, statInfo_unused = f.open(url, OpenFlags.READ)
    tend = time.time()
    log.info('msg="Invoked _xrootcmd" cmd="%s%s" url="%s" elapsedTimems="%.1f"' %
             (cmd, ('/' + subcmd if subcmd else ''), url, (tend-tstart)*1000))
    res = f.readline().decode('UTF-8').strip('\n').split('&')
    if len(res) == 3:    # we may only just get stdout: in that case, assume it's all OK
      rc = res[2]
      rc = rc[rc.find('=')+1:]
      if rc != '0':
        # failure: get info from stderr, log and raise
        msg = res[1][res[1].find('=')+1:]
        log.info('msg="Error with xroot command" cmd="%s" subcmd="%s" args="%s" error="%s" rc="%s"' % \
                 (cmd, subcmd, args, msg, rc.strip('\00')))
        raise IOError(msg)
  # all right, return everything that came in stdout
  return res[0][res[0].find('stdout=')+7:]


def _getfilepath(filepath, encodeamp=False):
  '''Map the given filepath into the target namespace by prepending the homepath (see storagehomepath in wopiserver.conf)'''
  return homepath + (filepath if not encodeamp else filepath.replace('&', '#AND#'))     # this is a special legacy encoding by eos


def init(inconfig, inlog):
  '''Init module-level variables'''
  global config         # pylint: disable=global-statement
  global log            # pylint: disable=global-statement
  global defaultstorage # pylint: disable=global-statement
  global homepath       # pylint: disable=global-statement
  config = inconfig
  log = inlog
  defaultstorage = config.get('xroot', 'storageserver')
  # prepare the xroot client for the default storageserver
  _getxrdfor(defaultstorage)
  if config.has_option('xroot', 'storagehomepath'):
    homepath = config.get('xroot', 'storagehomepath')
  else:
    homepath = ''


def stat(endpoint, filepath, userid):
  '''Stat a file via xroot on behalf of the given userid, and returns (size, mtime). Uses the default xroot API.'''
  filepath = _getfilepath(filepath, encodeamp=True)
  tstart = time.time()
  rc, statInfo = _getxrdfor(endpoint).stat(filepath + _eosargs(userid))
  tend = time.time()
  log.info('msg="Invoked stat" filepath="%s" elapsedTimems="%.1f"' % (filepath, (tend-tstart)*1000))
  if statInfo is None:
    raise IOError(rc.message.strip('\n'))
  if statInfo.flags & StatInfoFlags.IS_DIR > 0:
    raise IOError('Is a directory')
  return {'size': statInfo.size, 'mtime': statInfo.modtime}


def statx(endpoint, filepath, userid, versioninv=0):
  '''Get extended stat info (inode, filepath, userid, size, mtime) via an xroot opaque query on behalf of the given userid.
  If versioninv=1, the logic to support the version folder is not triggered.'''
  tstart = time.time()
  rc, info = _getxrdfor(endpoint).query(QueryCode.OPAQUEFILE, _getfilepath(filepath, encodeamp=True) + _eosargs(userid) + '&mgm.pcmd=stat')
  info = info.decode()
  log.info('msg="Invoked stat" filepath="%s"' % _getfilepath(filepath))
  if '[SUCCESS]' not in str(rc):
    raise IOError(str(rc).strip('\n'))
  if 'retc=2\\x00' in info:
    raise IOError('No such file or directory')   # convert ENOENT
  if 'retc=' in info:
    raise IOError(info.strip('\n'))
  statxdata = info.split()
  # we got now a full record according to https://gitlab.cern.ch/dss/eos/-/blob/master/mgm/XrdMgmOfs/fsctl/Stat.cc#L53
  if S_ISDIR(int(statxdata[3])):
    raise IOError('Is a directory')      # EISDIR
  if versioninv == 0:
    # classic statx info of the given file; endpoint is in the form `root://...` here, so we strip the protocol and the domain
    return {'inode': endpoint[7:-8] + '.' + statxdata[2],
            'filepath': filepath,
            'userid': statxdata[5] + ':' + statxdata[6],
            'size': int(statxdata[8]),
            'mtime': int(statxdata[12])}
  # now stat the corresponding version folder to get an inode invariant to save operations, see CERNBOX-1216
  verFolder = os.path.dirname(filepath) + os.path.sep + EOSVERSIONPREFIX + os.path.basename(filepath)
  rcv, infov = _getxrdfor(endpoint).query(QueryCode.OPAQUEFILE, _getfilepath(verFolder) + _eosargs(userid) + '&mgm.pcmd=stat')
  tend = time.time()
  infov = infov.decode()
  log.debug('msg="Invoked stat on version folder" endpoint="%s" filepath="%s" result="%s" elapsedTimems="%.1f"' % \
            (endpoint, _getfilepath(verFolder), infov, (tend-tstart)*1000))
  try:
    if '[SUCCESS]' not in str(rcv) or 'retc=' in infov:
      # the version folder does not exist: create it
      # cf. https://github.com/cernbox/revaold/blob/master/api/public_link_manager_owncloud/public_link_manager_owncloud.go#L127
      rcmkdir = _getxrdfor(endpoint).mkdir(_getfilepath(verFolder) + _eosargs(userid), MkDirFlags.MAKEPATH)
      log.debug('msg="Invoked mkdir on version folder" filepath="%s" rc="%s"' % (_getfilepath(verFolder), rcmkdir))
      if '[SUCCESS]' not in str(rcmkdir):
        raise IOError
      rcv, infov = _getxrdfor(endpoint).query(QueryCode.OPAQUEFILE, _getfilepath(verFolder) + _eosargs(userid) + '&mgm.pcmd=stat')
      infov = infov.decode()
      log.debug('msg="Invoked stat on version folder" filepath="%s" result="%s"' % (_getfilepath(verFolder), infov))
      if '[SUCCESS]' not in str(rcv) or 'retc=' in infov:
        raise IOError
    statxvdata = infov.split()
  except IOError:
    log.warn('msg="Failed to mkdir/stat version folder" rc="%s"' % rcv)
    statxvdata = statxdata
  # return the metadata of the given file, except for the inode that is taken from the version folder
  log.debug('msg="Invoked stat return" fileid="%s" filepath="%s"' % \
            (endpoint[7:-8] + '.' + statxvdata[2], _getfilepath(verFolder)))
  return {'inode': endpoint[7:-8] + '.' + statxvdata[2],
          'filepath': filepath,
          'userid': statxdata[5] + ':' + statxdata[6],
          'size': int(statxdata[8]),
          'mtime': int(statxdata[12])}


def setxattr(endpoint, filepath, userid, key, value):
  '''Set the extended attribute <key> to <value> via a special open on behalf of the given userid'''
  _xrootcmd(endpoint, 'attr', 'set', userid, 'mgm.attr.key=' + key + '&mgm.attr.value=' + str(value) + \
            '&mgm.path=' + _getfilepath(filepath, encodeamp=True))


def getxattr(endpoint, filepath, userid, key):
  '''Get the extended attribute <key> via a special open on behalf of the given userid'''
  res = _xrootcmd(endpoint, 'attr', 'get', userid, 'mgm.attr.key=' + key + '&mgm.path=' + _getfilepath(filepath, encodeamp=True))
  # if no error, the response comes in the format <key>="<value>"
  try:
    return res.split('"')[1]
  except IndexError:
    log.warning('msg="Failed to getxattr" filepath="%s" key="%s" res="%s"' % (filepath, key, res))
    return None


def rmxattr(endpoint, filepath, userid, key):
  '''Remove the extended attribute <key> via a special open on behalf of the given userid'''
  _xrootcmd(endpoint, 'attr', 'rm', userid, 'mgm.attr.key=' + key + '&mgm.path=' + _getfilepath(filepath, encodeamp=True))


def readfile(endpoint, filepath, userid):
  '''Read a file via xroot on behalf of the given userid. Note that the function is a generator, managed by Flask.'''
  log.debug('msg="Invoking readFile" filepath="%s"' % filepath)
  with XrdClient.File() as f:
    fileurl = _geturlfor(endpoint) + '/' + homepath + filepath + _eosargs(userid)
    tstart = time.time()
    rc, statInfo_unused = f.open(fileurl, OpenFlags.READ)
    tend = time.time()
    if not rc.ok:
      # the file could not be opened: check the case of ENOENT and log it as info to keep the logs cleaner
      if 'No such file or directory' in rc.message:
        log.info('msg="File not found on read" filepath="%s"' % filepath)
        yield IOError('No such file or directory')
      else:
        log.warning('msg="Error opening the file for read" filepath="%s" code="%d" error="%s"' % \
                    (filepath, rc.shellcode, rc.message.strip('\n')))
        yield IOError(rc.message)
    else:
      log.info('msg="File open for read" filepath="%s" elapsedTimems="%.1f"' % (filepath, (tend-tstart)*1000))
      chunksize = config.getint('io', 'chunksize')
      rc, statInfo = f.stat()
      chunksize = min(chunksize, statInfo.size)
      # the actual read is buffered and managed by the Flask server
      for chunk in f.readchunks(offset=0, chunksize=chunksize):
        yield chunk


def writefile(endpoint, filepath, userid, content, islock=False):
  '''Write a file via xroot on behalf of the given userid. The entire content is written
     and any pre-existing file is deleted (or moved to the previous version if supported).
     With islock=True, the write explicitly disables versioning, and the file is opened with
     O_CREAT|O_EXCL, preventing race conditions.'''
  size = len(content)
  log.debug('msg="Invoking writeFile" filepath="%s" userid="%s" size="%d" islock="%s"' % (filepath, userid, size, islock))
  f = XrdClient.File()
  tstart = time.time()
  rc, statInfo_unused = f.open(_geturlfor(endpoint) + '/' + homepath + filepath + _eosargs(userid, not islock, size),
                               OpenFlags.NEW if islock else OpenFlags.DELETE)
  tend = time.time()
  if not rc.ok:
    if islock and 'File exists' in rc.message:
      # racing against an existing file
      log.info('msg="File exists on write but islock flag requested" filepath="%s"' % filepath)
      raise IOError('File exists and islock flag requested')
    # any other failure is reported as is
    log.warning('msg="Error opening the file for write" filepath="%s" error="%s"' % (filepath, rc.message.strip('\n')))
    raise IOError(rc.message.strip('\n'))
  # write the file. In a future implementation, we should find a way to only update the required chunks...
  rc, statInfo_unused = f.write(content, offset=0, size=size)
  if not rc.ok:
    log.warning('msg="Error writing the file" filepath="%s" error="%s"' % (filepath, rc.message.strip('\n')))
    raise IOError(rc.message.strip('\n'))
  rc, statInfo_unused = f.truncate(size)
  if not rc.ok:
    log.warning('msg="Error truncating the file" filepath="%s" error="%s"' % (filepath, rc.message.strip('\n')))
    raise IOError(rc.message.strip('\n'))
  rc, statInfo_unused = f.close()
  if not rc.ok:
    log.warning('msg="Error closing the file" filepath="%s" error="%s"' % (filepath, rc.message.strip('\n')))
    raise IOError(rc.message.strip('\n'))
  log.info('msg="File written successfully" filepath="%s" elapsedTimems="%.1f" islock="%s"' % \
           (filepath, (tend-tstart)*1000, islock))


def renamefile(endpoint, origfilepath, newfilepath, userid):
  '''Rename a file via a special open from origfilepath to newfilepath on behalf of the given userid.'''
  _xrootcmd(endpoint, 'file', 'rename', userid, 'mgm.path=' + _getfilepath(origfilepath, encodeamp=True) + \
            '&mgm.file.source=' + _getfilepath(origfilepath, encodeamp=True) + \
            '&mgm.file.target=' + _getfilepath(newfilepath, encodeamp=True))


def removefile(endpoint, filepath, userid, force=0):
  '''Remove a file via a special open on behalf of the given userid.
     If force=1 or True, then pass the f option, that is skip the recycle bin.
     This is useful for lock files, but as it requires root access the userid is overridden.'''
  if force:
    userid = '0:0'
  _xrootcmd(endpoint, 'rm', None, userid, 'mgm.path=' + _getfilepath(filepath, encodeamp=True) + \
                                          ('&mgm.option=f' if force else ''))
