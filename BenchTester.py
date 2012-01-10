#!/usr/bin/env python

# This Source Code is subject to the terms of the Mozilla Public License
# version 2.0 (the "License"). You can obtain a copy of the License at
# http://mozilla.org/MPL/2.0/.

import sys
import os
import argparse
import sqlite3
import subprocess
import mercurial, mercurial.ui, mercurial.hg, mercurial.commands
import time

gTableSchemas = [
  # Builds - info on builds we have tests for
  '''CREATE TABLE IF NOT EXISTS
      "benchtester_builds" ("id" INTEGER PRIMARY KEY NOT NULL,
                          "name" VARCHAR NOT NULL,
                          "time" DATETIME NOT NULL)''',
                          
  # Tests - tests that have been run and against which build
  '''CREATE TABLE IF NOT EXISTS
      "benchtester_tests" ("id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                         "name" VARCHAR NOT NULL,
                         "time" DATETIME NOT NULL,
                         "build_id" INTEGER NOT NULL)''',
                         
  # Data - datapoints from tests
  '''CREATE TABLE IF NOT EXISTS
      "benchtester_data" ("test_id" INTEGER NOT NULL,
                        "datapoint" VARCHAR NOT NULL,
                        "value" INTEGER,
                        PRIMARY KEY ("test_id", "datapoint"))'''
];

# TODO:
# - doxygen
# - add more self.info checkpoints
# - Build/mozmill output might need logging. --autobuild-log?
#
# - Make work on OS X/Windows:
#   - Make autobuild detect when to use pymake
#   - msvc builds...?
#   - hardcoded 'dist/bin/firefox' not valid for other platforms

# Runs the mozmill memory tests and generates/updates a json data object with
# results
# - Currently used to graph the data on areweslimyet.com

class BenchTest():
  def __init__(self, parent):
    self.tester = parent
    self.name = "Unconfigured Test Module"
    
  def run_test(self, testname, testvars={}):
    return self.error("run_test() not defined")
    
  def setup(self):
    return True
    
  def error(self, msg):
    return self.tester.error("[%s] %s" % (self.name, msg))
  
  def warn(self, msg):
    return self.tester.warn("[%s] %s" % (self.name, msg))
  
  def info(self, msg):
    return self.tester.info("[%s] %s" % (self.name, msg))

# The main class for running tests
class BenchTester():
    
  def info(self, msg):
    self.log('info', msg)
    
  def error(self, msg):
    self.log('error', msg)
    return False
    
  def warn(self, msg):
    self.log('warning', msg)
    
  def log(self, type, msg, timestamp = None, noprint = False):
    if not timestamp:
      timestamp = time.clock() - self.starttime
      
    if self.logfile:
      self.logfile.write("%.2f :: %s :: %s\n" % (timestamp, type.upper(), msg))
      self.logfile.flush()
    elif not self.ready:
      # Cache lines until setup is called to open the logfile
      if not hasattr(self, 'logcache'): self.logcache = []
      self.logcache.append((type, msg, timestamp))
    
    if not noprint:
      self.out.write("[%.2f] %s: %s\n" % (timestamp, type.upper(), msg))
  
  def build(self):
    if not self.autobuild or self.built: return True
    
    if not self.ready:
      return self.error("build() called before successful setup()")
    if not self.autobuild:
      return self.error("build() called when not configured for autobuild")
    try:
      mozconfig = os.path.abspath(self.mozconfig)
    except:
      return self.error("Failed to resolve '%s' to an absolute path" % self.mozconfig)
    if not os.path.exists(mozconfig):
        return self.error("File does not exist: %s" % self.mozconfig)
      
    newenv = os.environ.copy()
    newenv['MOZCONFIG'] = mozconfig
    
    self.info("Beginning build")
    try:
      proc = subprocess.Popen(['make', '-f', 'client.mk'],
                              env=newenv,
                              cwd=self.repopath,
                              stderr=subprocess.STDOUT,
                              stdout=subprocess.PIPE)
    except Exception, e:
      return self.error("Build triggered an exception: %s" % e)
    
    # Wait for EOF, logging if desired
    while data = proc.stdout.read(1024):
      if self.autobuild_logfile:
        self.autobuild_logfile.write(data)
        
    ret = proc.wait()
    if ret == 0:
      self.info("Build successful")
    else:
      return self.error("Build failed")
      
    self.built = True
    return True
    
  def run_test(self, testname, testtype, testvars={}):
    if not self.ready:
      return self.error("run_test() called before setup")
    
    # Make sure checkout and build have been done
    if not self.checkout() and self.build():
      return False
    
    if self.modules.has_key(testtype):
      self.info("Passing test '%s' to module '%s'" % (testname, testtype))
      return self.modules[testtype].run_test(testname, testvars)
    else:
      return self.error("Test '%s' is of unknown type '%s'" % (testname, testtype))
  
  # Modules are named 'SomeModule.py' and have a class named 'SomeModule' based on BenchTest
  def load_module(self, modname):
    if self.ready:
      return self.error("Modules must be loaded before setup()")
      
    if self.modules.has_key(modname): return True
    
    self.info("Loading module '%s'" % (modname))
    try:
      module = __import__(modname)
      self.modules[modname] = vars(module)[modname](self)
    except Exception, e:
      return self.error("Failed to load module '%s', Exception '%s': %s" % (modname, type(e), e))
        
    return True
    
  def add_test_results(self, testname, datapoints):
    if (not self.sqlite and self.args['sqlitedb']):
      if not self._open_db():
        self.error("Database open failed, wont be saving results")
        self.args['sqlitedb'] = False
    
    if not testname or not len(datapoints):
      return self.error("Invalid use of addDataPoint()")
    
    timestamp = time.time()
    
    for datapoint, val in datapoints.iteritems():
      self.info("Datapoint: Test '%s', Datapoint '%s', Value '%s'" % (testname, datapoint, val))
    if self.sqlite:
      try:
        cur = self.sqlite.cursor()
        cur.execute("INSERT INTO `benchtester_tests` (`name`, `time`, `build_id`) VALUES (?, ?, ?)", (testname, int(timestamp), self.build_id))
        cur.execute("SELECT last_insert_rowid()")
        testid = cur.fetchone()[0]
        for datapoint, val in datapoints.iteritems():
          cur.execute("INSERT INTO `benchtester_data` (`test_id`, `datapoint`, `value`) VALUES (?, ?, ?)", (testid, datapoint, val))
        self.sqlite.commit()
      except Exception, e:
        self.error("Failed to insert data into sqlite, got '%s': %s" % (type(e), e))
        self.sqlite.rollback()
        return False
    return True
    
  def checkout(self):
    if not self.autobuild or self.checked_out: return True
    
    if not self.ready:
      return self.error("checkout() called before successful setup()")
    
    if self.autobuild_pull:
      self.info("Beginning mercurial pull")
      try:
        self.hg_ui.pushbuffer()
        self.hg_ui.readconfig(os.path.join(self.repopath, ".hg", "hgrc"))
        mercurial.commands.pull(self.hg_ui, self.repo, update=True, check=True)
        result = self.hg_ui.popbuffer()
        if self.autobuild_logfile:
          self.autobuild_logfile.write(result)
      except Exception, e:
        return self.error("Failed to pull mercurial repo, Exception '%s': %s" % (type(e), e))
    
    try:
      self.hg_ui.pushbuffer()
      if self.args['autobuild_commit']:
        commitname=self.args['autobuild_commit']
        docheckout = True
      else:
        commitname="." # use currently checked out files
        docheckout = False
      self.info("Looking up commit info in autobuild repo for '%s'" % commitname)
      mercurial.commands.log(self.hg_ui, self.repo, rev=[commitname], template="{node} {date}", date="", user=None, follow=None)
      commitinfo = self.hg_ui.popbuffer().split()
    except Exception, e:
      return self.error("Failed to get info about specified commit in autobuild repository: %s" % e)
    self.commit = commitinfo[0]
    # If not set, seed testname/time with defaults from this commit
    committime = commitinfo[1].split('.')[0] # {date} produces a timestamp of format '123234234.0-3600'
    if not self.buildname: self.buildname = self.commit
    if not self.buildtime: self.buildtime = committime
    self.info("Autobuilding commit '%s' with timestamp '%s'" % (self.commit, committime))
    
    if not docheckout:
      self.info("No commit specified, not checking anything out and using existing tree")
      self.checked_out = True
      return True

    self.info("Beginning mercurial checkout")
    try:
      self.hg_ui.pushbuffer()
      mercurial.commands.update(self.hg_ui, self.repo, self.commit, check=True)
      result = self.hg_ui.popbuffer()
      if self.autobuild_logfile:
        self.autobuild_logfile.write(result)
    except Exception, e:
      return self.error("Failed to checkout revision '%s', Error: %s" % (self.commit, e))
    self.info("Succesfully checked out revision '%s'" % self.commit)
    return True
    
  def __init__(self, out=sys.stdout):
    self.starttime = time.clock()
    self.ready = False
    self.args = {}
    self.argparser = argparse.ArgumentParser(description='Run automated benchmark suite, optionally adding datapoints to a sqlite database')
    self.arglist = {}
    self.out = out
    self.modules = {}
    self.logfile = None
    self.buildtime = None
    self.autobuild_logfile = None
    self.buildname = None
    self.sqlite = False
    self.checked_out = False
    self.built = False
    
    # These can be passed to setup() like so:
    #   mytester.setup({'binary': 'blah', 'autobuild_repo': 'blee'})
    # OR you can call mytester.parseArgs() on a command-line formatted arg list (sys.argv) to extract
    #   them as needed.
    self.add_argument('-b', '--binary',              help='The binary (either in the current PATH or a full path) to test')
    self.add_argument('--autobuild-repo',            help='Automatically build and test a commit using this local repo')
    self.add_argument('--autobuild-commit',          help='In autobuild mode, the commit (changeset) to checkout, otherwise \
                                                           defaults to currently checked out revision (including uncommited \
                                                           changes!)')
    self.add_argument('--autobuild-mozconfig',       help='Path to a mozconfig to use for test builds in autobuild mode')
    self.add_argument('--autobuild-pull',            help='Run a pull on the repository before checking out commit. Useful \
                                                           with "--autobuild-commit tip"',
                                                     action='store_true')
    self.add_argument('--autobuild-objdir',          help='Path to object directory (absolute or relative to repo) that \
                                                           given mozconfig will output')
    self.add_argument('--autobuild-log',             help='Dump build output to given file' 
    self.add_argument('--buildname',                 help='The name of this firefox build. If omitted, attempts to use the \
                                                           commit id from the mercurial respository the binary resides \
                                                           in, or the changeset id in autobuild mode')
    self.add_argument('--buildtime',                 help='The unix timestamp to assign to this build \
                                                           build. If omitted, attempts to use the commit timestamp \
                                                           from the mercurial repository the binary resides in, or of \
                                                           the changeset being tested in autobuild mode')
    self.add_argument('--test-module', '-m',         help='Load the specified test module (from libs). You must load at least one module to have tests',
                                                     action='append')
    self.add_argument('-l', '--logfile',             help='Log to given file')
    self.add_argument('-s', '--sqlitedb',            help='Merge datapoint into specified sqlite database')
    
    self.info("BenchTester instantiated")
    
  def add_argument(self, *args, **kwargs):
    act = self.argparser.add_argument(*args, **kwargs)
    if kwargs.has_key('default'):
      self.args[act.dest] = kwargs['default']
  
  # Parses commandline arguments, *AND* loads the modules specified on them,
  #   such that their arguments can be known/parsed. Does not prevent loading of
  #   more modules later on.
  # - returns a args dict suitable for passing to setup().
  # - If handle_exceptions is false, will let argparser failures fall through.
  #   Otherwise, prints an error.
  # - The "test_module" argument is returned, but not used by setup, and is
  #   useful for seeing what modules the commandline just caused to load
  def parse_args(self, rawargs=sys.argv[1:]):
    self.info("Parsing arguments...")
    try:
      args = vars(self.argparser.parse_known_args(rawargs)[0])
      # Modules can add more arguments, so load the ones specified
      # and re-parse
      if args['test_module']:
        for m in args['test_module']:
          self.load_module(m)
      args = vars(self.argparser.parse_args(rawargs))
      return args
    except SystemExit, e:
        return False
  
  def __del__(self):
    # In case we exception out mid transaction or something
    if (hasattr(self, 'sqlite') and self.sqlite):
      self.sqlite.rollback()
    
  def _open_db(self):
    if self.sqlite: return True
    
    self.info("Setting up SQLite")
    if not self.buildname or not self.buildtime:
      return self.error("Cannot use db without a buildname and buildtime set")
    try:
      sql_path = os.path.abspath(self.args['sqlitedb'])
      self.sqlite = sqlite3.connect(sql_path)
      cur = self.sqlite.cursor()
      for schema in gTableSchemas:
        cur.execute(schema)
      # Create build ID
      cur.execute("SELECT `time`, `id` FROM `benchtester_builds` WHERE `name` = ?", [ self.buildname ])
      buildrow = cur.fetchone()
      if not buildrow:
        self.info("No previous tests for this build, creating new build record")
        cur.execute("INSERT INTO `benchtester_builds` (`name`, `time`) VALUES (?, ?)", (self.buildname, int(self.buildtime)))
        cur.execute("SELECT last_insert_rowid()")
        self.build_id = cur.fetchone()[0]
      elif buildrow[0] != int(self.buildtime):
        self.sqlite.rollback()
        return self.error("Build '%s' already exists in the database, but with a different timestamp! (%s)" % (self.buildname, buildrow[0]))
      else:
        self.build_id = buildrow[1]
        self.info("Found build record")
      self.sqlite.commit()
    except Exception, e:
      self.error("Failed to setup sqliteDB '%s': %s - %s\n" % (self.args['sqlitedb'], type(e), e))
      self.sqlite = None
      return False
    
    return True

  def setup(self, args):
    self.info("Performing setup")
    self.hg_ui = mercurial.ui.ui()
    
    # args will already contain defaults from add_argument calls
    self.args.update(args)
    
    # Open logfile
    if self.args['logfile']:
      self.logfile_path = None
      try:
        self.logfile_path = os.path.abspath(self.args['logfile'])
        self.logfile = open(self.logfile_path, 'w')
      except Exception, e:
        return self.error("Unable to open logfile '%s' (%s)" % (self.args['logfile'], self.logfile_path))
      # Print any lines that occured before the logfile opened
      if hasattr(self, 'logcache'):
        self.logcache.reverse()
        while len(self.logcache):
          time, msg, timestamp = self.logcache.pop()
          self.log(time, msg, timestamp, True)
        self.logcache = None
    self.info("Opened logfile")
    
    # Setup autobuild
    if self.args['autobuild_repo']:
      if not self.args['autobuild_mozconfig'] or not self.args['autobuild_objdir']:
        return self.error("--autobuild-mozconfig and --autobuild-objdir are required for autobuild mode")
      self.objdirpath = self.args['autobuild_objdir']
      
      try:
        self.repopath = os.path.abspath(self.args['autobuild_repo'])
        self.repo = mercurial.hg.repository(self.hg_ui, self.repopath)
      except Exception, e:
        return self.error("Error opening '%s' as a mercurial repository: %s" % (self.args['autobuild_repo'], e))
      
      try:
        self.mozconfig = os.path.abspath(self.args['autobuild_mozconfig'])
      except:
        return self.error("Failed to resolve file to an absolute path: %s" % e)
      
      if not os.path.exists(self.mozconfig):
        return self.error("Given mozconfig does not exist (%s)" % self.mozconfig)
      
      if self.args['autobuild_log']:
        try:
          self.autobuild_logfile = open(self.args['autobuild_log'], 'w')
        except Exception, e:
          self.error("Failed to open log file '%s' for writing" % self.args['autobuild_log'])
        self.info("Opened build log file")
      self.autobuild_pull = self.args['autobuild_pull']
      self.autobuild = True
    else:
      if self.args['autobuild_log'] or self.args['autobuild_commit'] or self.args['autobuild_mozconfig'] or self.args['autobuild_objdir']:
        return self.error("Autobuild options do not make sense without --autobuild-repo")
      self.autobuild = False
    
    # Check that binary or autobuild is set
    if self.autobuild:
      if self.args['binary']:
        return self.error("Specifying a binary does not make sense in conjunction with autobuild")
      try:
        # FIXME
        # This isn't right for non-Linux
        self.binary = os.path.abspath(os.path.join(self.args['autobuild_objdir'], 'dist', 'bin', 'firefox'))
      except:
        return self.error("Couldn't resolve absolute path of provided objdir (%s)" % self.args['autobuild_objdir'])
      
    else:
      if not self.args['binary']:
        return self.error("--binary is required if not using autobuild mode, see --help")
      try:
        self.binary = os.path.abspath(self.args['binary'])
      except:
        self.binary = False
      if not self.binary or not os.path.exists(self.binary):
        return self.error("Unable to access binary '%s' (abs: '%s')\n" % (self.args['binary'], self.binary if self.binary else "Cannot resolve"))
    
    # Set commit name/timestamp
    if (self.args['buildname']):
      self.buildname = self.args['buildname'].strip()
    if (self.args['buildtime']):
      self.buildtime = self.args['buildtime'].strip()

    
    # Try to autodetect commitname/time if given a binary in a repo
    if not self.autobuild and (not self.buildname or not self.buildtime):
      try:
        hg_repo = mercurial.hg.repository(self.hg_ui, os.path.dirname(self.binary))
      except:
        hg_repo = None
      if hg_repo:
        try:
          self.info("Binary is in a hg repo, attempting to detect build info")
          self.hg_ui.pushbuffer()
          mercurial.commands.tip(self.hg_ui, hg_repo, template="{node} {date}")
          tipinfo = self.hg_ui.popbuffer().split()
          hg_changeset = tipinfo[0]
          # Date is a float (truncate to int) of format 12345.0[+/-]3600 where 3600 is timezone info
          hg_date = tipinfo[1].split('.')[0]
          if not self.buildname:
            self.buildname = hg_changeset
            self.info("No build name given, using %s from repo binary is in" % self.buildname)
          if not self.buildtime:
            self.buildtime = hg_date
            self.info("No build time given, using %s from repo binary is in" % self.buildtime)
        except Exception as e:
          self.error("Found a Hg repo, but failed to get  changeset/timestamp. \
                      You may need to provide these manually with --buildname, --buildtime\
                      \nError was: %s" % (e));
    
    # Sanity checks
    if (self.sqlite):
      if (not self.buildname or not len(self.buildname)):
        self.error("Must provide a name for this build via --buildname in order to log to sqlite")
        return False
      
      try:
        inttime = int(self.buildtime, 10)
      except:
        inttime = None
      if (not inttime or str(inttime) != self.buildtime or inttime < 1):
        self.error("--buildtime must be set to a unix timestamp in order to log to sqlite")
        return False
    
    self.failed_modules = {}
    for m in self.modules:
      if not self.modules[m].setup():
        self.error("Failed to setup module %s!" % m)
        self.failed_modules[m] = self.modules[m]
    for m in self.failed_modules:
      del self.modules[m]
    
    self.ready = True
    self.info("Setup successful")
    return True
