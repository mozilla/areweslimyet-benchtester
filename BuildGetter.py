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

gBuildClasses = {}

socket.setdefaulttimeout(30)

# TODO
# This currently selects the linux-64 (non-pgo) build
# hardcoded at a few spots. This will need to be changed for non-linux testing

def stat(msg):
  sys.stderr.write("%s\n" % msg);

##
## Utility
##

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
  stat("Checking directory %s" % dirname)
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

  stat("Got build info: %s" % filedat)

  m = re.search('^[0-9]{14}', filedat)
  timestamp = int(time.mktime(time.strptime(m.group(0), '%Y%m%d%H%M%S')))
  m = re.search('([0-9a-z]{12})$', filedat)
  rev = m.group(1)
  nightlyfile = infofile[:-4] + ".tar.bz2"

  return (timestamp, rev, nightlyfile)

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
  def prepare(self):
    raise Exception("Attempt to call method on abstract base class")
  def cleanup(self):
    raise Exception("Attempt to call method on abstract base class")
  def get_revision(self):
    raise Exception("Attempt to call method on abstract base class")
  def get_buildtime(self):
    raise Exception("Attempt to call method on abstract base class")
  def get_binary(self):
    raise Exception("Attempt to call method on abstract base class")

# Abstract class with shared helpers for TinderboxBuild/NightlyBuild
class FTPBuild(Build):
  def prepare(self):
    self._fetch()
    stat("Extracting build")
    self._extracted = _extract_build(self._file)
    self._file.close()
    self._prepared = True

  def cleanup(self):
    if self._prepared:
      self._prepared = False
      shutil.rmtree(self._extracted)

  def get_revision(self):
    if not hasattr(self, '_revision'):
      raise Exception("Build is not prepared")
    return self._revision

  def get_binary(self):
    if not self._prepared:
      raise Exception("Build is not prepared")
    # FIXME More hard-coded linux stuff
    return os.path.join(self._extracted, "firefox", "firefox")

  def get_buildtime(self):
    if not hasattr(self, '_timestamp'):
      raise Exception("Build is not prepared")
    return self._timestamp
    
# A nightly build. Initialized with a date() object or a YYYY-MM-DD string
class NightlyBuild(FTPBuild):
  def __init__(self, date):
    self._prepared = False
    self._date = date
    
  # Get this build from ftp.m.o
  def _fetch(self):
    month = self._date.month
    day = self._date.day
    year = self._date.year
    stat("Looking up nightly for %s/%s, %s" % (month, day, year))

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
      raise Exception("Failed to find any nightly directory for date %s/%s/%s" % (month, day, year))

    stat("Nightly directories are: %s" % ', '.join(nightlydirs))

    for x in nightlydirs:
      (timestamp, revision, filename) = _ftp_check_build_dir(ftp, x)
      if revision:
        break

    if not revision:
      raise Exception("Couldn't find any directory with info on this build :(")

    nightlyfile = _ftp_get(ftp, filename)
    ftp.close()
    self._timestamp = timestamp
    self._revision = revision
    self._file = nightlyfile

gBuildClasses['nightly'] = NightlyBuild

# A tinderbox build from ftp.m.o. Initialized with a timestamp to build
class TinderboxBuild(FTPBuild):
  def __init__(self, timestamp):
    self._timestamp = int(timestamp)
    self._prepared = False

  def _fetch(self):
    ftp = ftplib.FTP('ftp.mozilla.org')
    ftp.login()
    ftp.voidcmd('CWD /pub/firefox/tinderbox-builds/mozilla-central-linux64/')
    (self._timestamp, self._revision, filename) = _ftp_check_build_dir(ftp, self._timestamp)
    if not self._timestamp:
      raise Exception("Tinderbox build %s not found on ftp.m.o" % timestamp)

    self._file = _ftp_get(ftp, filename)
    ftp.close()

gBuildClasses['tinderbox'] = TinderboxBuild
