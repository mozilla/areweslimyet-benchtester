#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright © 2012 Mozilla Corporation

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Helpers for building/testing many builds from either ftp.m.o nightly/tinderbox,
# or via autobuild

import os
import sys
import ftplib
import time
import re
import socket
import cStringIO
import shutil
import tarfile
import tempfile
import datetime
import subprocess

output = sys.stdout

socket.setdefaulttimeout(30)

# TODO
# This currently selects the linux-64 (non-pgo) build
# hardcoded at a few spots. This will need to be changed for non-linux testing

def _stat(msg):
  output.write("[BuildGetter] %s\n" % msg);

##
## Utility
##

def _subprocess(environment, command, cwd, logfile):
  newenv = os.environ.copy()
  newenv.update(environment)
  _stat("Running command \"%s\" in \"%s\" with env \"%s\"" % (command, cwd, environment))

  proc = subprocess.Popen(command,
                          env=newenv,
                          cwd=cwd,
                          stderr=subprocess.STDOUT,
                          stdout=subprocess.PIPE)

  # Wait for EOF, logging if desired
  while True:
    data = proc.stdout.read(1024)
    if not data: break
    if logfile:
      logfile.write(data)

  return proc.wait()

# Given a firefox build file handle, extract it to a temp directory, return that
def _extract_build(fileobject):
  # cross-platform FIXME, this is hardcoded to .tar.bz2 at the moment
  ret = tempfile.mkdtemp("BuildGetter_firefox")
  tar = tarfile.open(fileobj=fileobject, mode='r:bz2')
  tar.extractall(path=ret)
  tar.close()
  return ret

##
## Working with ftp.m.o
##

# Reads a file, returns the blob
def _ftp_get(ftp, filename):
  # (We use readfile.filedat temporarily because of py2's lack of proper scoping
  #  for nested functions)
  def readfile(line):
      readfile.filedat.write(line)
  readfile.filedat = cStringIO.StringIO()

  ftp.retrbinary('RETR %s' % filename, readfile)

  # Python2 didn't have any design flaws. None, I say!

  readfile.filedat.seek(0)
  return readfile.filedat

# Returns false if there's no linux-64 build here,
# otherwise returns a tuple of (timestamp, revision, filename)
def _ftp_check_build_dir(ftp, dirname):
  global infofile
  _stat("Checking directory %s" % dirname)
  infofile = False
  def findinfofile(line):
    global infofile
    if line.startswith('firefox') and line.endswith('linux-x86_64.txt'):
      infofile = line

  ftp.voidcmd('CWD %s' % dirname)
  ftp.retrlines('NLST', findinfofile)
  if not infofile:
    ftp.voidcmd('CwD ..')
    return False

  #
  # read and parse info file
  #

  fileio = _ftp_get(ftp, infofile)
  filedat = fileio.getvalue()
  fileio.close()

  _stat("Got build info: %s" % filedat)

  m = re.search('^[0-9]{14}', filedat)
  timestamp = int(time.mktime(time.strptime(m.group(0), '%Y%m%d%H%M%S')))
  m = re.search('([0-9a-z]{12})$', filedat)
  rev = m.group(1)
  nightlyfile = infofile[:-4] + ".tar.bz2"

  return (timestamp, rev, nightlyfile)

# Returns a list of commit IDs between two revisions, inclusive. If pullfirst is
# set, pull before checking
def get_hg_range(repodir, firstcommit, lastcommit, pullfirst=False):
    # Setup Hg
    import mercurial, mercurial.ui, mercurial.hg, mercurial.commands
    hg_ui = mercurial.ui.ui()
    repo = mercurial.hg.repository(hg_ui, repodir)
    hg_ui.readconfig(os.path.join(repodir, ".hg", "hgrc"))
    
    # Pull
    if pullfirst:
      hg_ui.pushbuffer()
      mercurial.commands.pull(hg_ui, repo, update=True, check=True)
      result = hg_ui.popbuffer()

    # Get revisions
    hg_ui.pushbuffer()
    mercurial.commands.log(hg_ui, repo, rev=[ "%s:%s" % (firstcommit, lastcommit) ], template="{node} ", date="", user=None, follow=None)
    return hg_ui.popbuffer().split(' ')[:-1]
  
# Gets a list of TinderboxBuild objects for all builds on ftp.m.o within
# specified date range
def get_tinderbox_builds(starttime = 0, endtime = int(time.time())):
  ftp = ftplib.FTP('ftp.mozilla.org')
  ftp.login()
  ftp.voidcmd('CWD /pub/firefox/tinderbox-builds/mozilla-central-linux64/')

  def get(line):
    try:
      x = int(line)
      if x >= starttime and x <= endtime:
        get.ret.append(x)
    except: pass
  get.ret = []
  ftp.retrlines('NLST', get)
  
  ret = []
  for x in sorted(get.ret):
    ret.append(TinderboxBuild(x))
  
  return ret

#
# Build classes
#

# Abstract base class
class Build():
  # Downloads or builds and extracts the build to a temporary directory
  def prepare(self):
    raise Exception("Attempt to call method on abstract base class")
  def cleanup(self):
    raise Exception("Attempt to call method on abstract base class")
  def get_revision(self):
    raise Exception("Attempt to call method on abstract base class")
  def get_buildtime(self):
    raise Exception("Attempt to call method on abstract base class")
  # Requires prepare()'d
  def get_binary(self):
    raise Exception("Attempt to call method on abstract base class")

# Abstract class with shared helpers for TinderboxBuild/NightlyBuild
class FTPBuild(Build):
  def prepare(self):
    if not self._revision:
      return False

    ftp = ftplib.FTP('ftp.mozilla.org')
    ftp.login()

    ftpfile = _ftp_get(ftp, self._filename)
    ftp.close()
    
    _stat("Extracting build")
    self._extracted = _extract_build(ftpfile)
    ftpfile.close()
    self._prepared = True
    return True

  def cleanup(self):
    if self._prepared:
      self._prepared = False
      shutil.rmtree(self._extracted)
    return True

  def get_revision(self):
    return self._revision

  def get_binary(self):
    if not self._prepared:
      raise Exception("Build is not prepared")
    # FIXME More hard-coded linux stuff
    return os.path.join(self._extracted, "firefox", "firefox")

  def get_buildtime(self):
    return self._timestamp

# A build that needs to be compiled
# repo - The local repo to use to build (must be cloned first)
# commit - If set, checkout this commit
# mozconfig - The mozconfig file to use to build
# pull - If true, pull before building/checking-out
# objdir - the object directory said mozconfig will create
# log - If set, where to put build spew
class CompileBuild(Build):
  def __init__(self, repo, mozconfig, objdir, pull=False, commit=None, log=None):
    self._repopath = repo
    self._commit = None
    self._mozconfig = mozconfig
    self._pull = pull
    self._objdir = objdir
    self._log = log
    self._logfile = None
    self._checkout = True if commit else False
    ##
    ## Get info about commit
    ##
    import mercurial, mercurial.ui, mercurial.hg, mercurial.commands
    hg_ui = mercurial.ui.ui()
    repo = mercurial.hg.repository(hg_ui, self._repopath)
    
    _stat("Getting commit info")
    commitname = commit if commit else "."
    hg_ui.pushbuffer()
    mercurial.commands.log(hg_ui, repo, rev=[commitname], template="{node} {date}", date="", user=None, follow=None)
    commitinfo = hg_ui.popbuffer().split()
    self._commit = commitinfo[0]
    # If not set, seed testname/time with defaults from this commit
    self._committime = commitinfo[1].split('.')[0] # {date} produces a timestamp of format '123234234.0-3600'
    _stat("Commit is %s @ %s" % (self._commit, self._committime))
    
  def prepare(self):
    ##
    ## Sanity checks, open log
    ##
    if not os.path.exists(self._mozconfig):
      raise Exception("Mozconfig given to CompileBuild does not exist")
    if not os.path.exists(self._repopath) or not os.path.exists(os.path.join(self._repopath, ".hg")):
      raise Exception("Given repo does not exist or is not a mercurial repo")
    if self._log:
      self._logfile = open(self._log, 'w')

    ##
    ## Setup HG, pull if wanted
    ##
    import mercurial, mercurial.ui, mercurial.hg, mercurial.commands
    hg_ui = mercurial.ui.ui()
    repo = mercurial.hg.repository(hg_ui, self._repopath)
    
    if self._pull:
      _stat("Beginning mercurial pull")
      hg_ui.pushbuffer()
      hg_ui.readconfig(os.path.join(self._repopath, ".hg", "hgrc"))
      mercurial.commands.pull(hg_ui, repo, update=True, check=True)
      result = hg_ui.popbuffer()
      if self._logfile:
        self._logfile.write(result)

    ##
    ## Checkout if needed
    ##
    if self._checkout:
      _stat("Performing checkout")
      hg_ui.pushbuffer()
      mercurial.commands.update(hg_ui, repo, node=self._commit, check=True)
      result = hg_ui.popbuffer()
      if self._logfile:
        self._logfile.write(result)

    _stat("Building")
    # Build
    def build():
      return _subprocess({ 'MOZCONFIG' : os.path.abspath(self._mozconfig) }, [ 'make', '-f', 'client.mk' ], self._repopath, self._logfile)

    ret = build()
    if ret != 0 and os.path.exists(self._objdir):
      _stat("Build failed, trying again with fresh object directory")
      shutil.rmtree(self._objdir)
      ret = build()
      
    if ret != 0:
      _stat("Build with fresh object directory failed")
      return False

    _stat("Packaging")
    # Package
    ret = _subprocess({}, [ 'make', 'package' ], self._objdir, self._logfile)
    if ret != 0:
      _stat("Package failed")
      return False
      
    # Find package file
    # FIXME linux-specific
    distdir = os.path.join(self._objdir, "dist")
    files = os.listdir(distdir)
    package = None
    for f in files:
      if f.startswith("firefox-") and f.endswith(".tar.bz2"):
        package = os.path.join(distdir, f)
        break
    if not package:
      _stat("Failed to find built package")
      return False

    # Extract
    package = open(package, 'r')
    self._extracted = _extract_build(package)
    package.close()

    self._prepared = True
    return True

  def cleanup(self):
    if self._prepared:
      shutil.rmtree(self._extracted)
      self._prepared = False
    return True;

  def get_buildtime(self):
    return self._committime

  def get_binary(self):
    if not self._prepared:
      raise Exception("CompileBuild is not prepared")
    # More linux-specific stuff
    return os.path.join(self._extracted, "firefox", "firefox")

  def get_revision(self):
    return self._commit

# A nightly build. Initialized with a date() object or a YYYY-MM-DD string
class NightlyBuild(FTPBuild):
  def __init__(self, date):
    self._prepared = False
    self._date = date
    self._timestamp = None
    self._revision = None
    month = self._date.month
    day = self._date.day
    year = self._date.year
    _stat("Looking up nightly for %s/%s, %s" % (month, day, year))

    # Connect, CD to this month's dir
    ftp = ftplib.FTP('ftp.mozilla.org')
    ftp.login()
    nightlydir = 'pub/firefox/nightly/%i/%02i' % (year, month)
    ftp.voidcmd('CWD %s' % nightlydir)

    # Find the appropriate YYYY-MM-DD-??-mozilla-central directory. There may be
    # multiple if the builds took over an hour
    nightlydirs = []
    def findnightlydir(line):
      x = line.split('-')
      if x[-2:] == [ 'mozilla', 'central' ] and int(x[0]) == year and int(x[1]) == month and int(x[2]) == day:
        nightlydirs.append(line)

    rawlist = ftp.retrlines('NLST', findnightlydir)

    if not len(nightlydirs):
      return;

    _stat("Nightly directories are: %s" % ', '.join(nightlydirs))

    for x in nightlydirs:
      ret = _ftp_check_build_dir(ftp, x)
      if ret:
        (timestamp, revision, filename) = ret
        self._filename = "%s/%s/%s" % (nightlydir, x, filename)
        break

    if not revision:
      return;

    ftp.close()
    self._timestamp = timestamp
    self._revision = revision

# A tinderbox build from ftp.m.o. Initialized with a timestamp to build
class TinderboxBuild(FTPBuild):
  def __init__(self, timestamp):
    self._timestamp = int(timestamp)
    self._prepared = False
    self._revision = None

    basedir = "/pub/firefox/tinderbox-builds/mozilla-central-linux64"
    ftp = ftplib.FTP('ftp.mozilla.org')
    ftp.login()
    ftp.voidcmd('CWD %s' % (basedir,))
    ret = _ftp_check_build_dir(ftp, self._timestamp)
    if not ret:
      _stat("WARN: Tinderbox build %s was not found" % (self._timestamp,))
      return
    (timestamp, self._revision, filename) = ret

    self._filename = "%s/%s" % (basedir, filename)
    ftp.close()
    return True
