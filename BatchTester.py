#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright © 2012 Mozilla Corporation

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import sys
import argparse
import time
import datetime
import multiprocessing
import socket
import shlex
import platform
import sqlite3
import json

import BuildGetter

##
##
##

is_win = platform.system() == "Windows"

##
## Utility
##

def parse_nightly_time(string):
  string = string.split('-')
  if (len(string) != 3):
    raise Exception("Could not parse %s as a YYYY-MM-DD date")
  return datetime.date(int(string[0]), int(string[1]), int(string[2]))

# Grab the first file (alphanumerically) from the batch folder,
# delete it and return its contents
def get_queued_job(dirname):
  batchfiles = os.listdir(dirname)
  if len(batchfiles):
    bname = os.path.join(dirname, sorted(batchfiles)[0])
    bfile = open(bname, 'r')
    bcmd = bfile.read()
    bfile.close()
    os.remove(bname)
    return bcmd
  return False

# Wrapper for BuildGetter.Build objects
class BatchBuild():
  def __init__(self, build, revision):
    self.build = build
    self.revision = revision
    self.num = None
    self.task = None
    self.note = None
    self.started = None
    self.finished = None

  def deserialize(buildobj, args):
    if buildobj['type'] == 'compile':
      build = BuildGetter.CompileBuild(args.get('repo'), args.get('mozconfig'), args.get('objdir'), pull=True, commit=buildobj['revision'], log=None)
    elif buildobj['type'] == 'tinderbox':
      build = BuildGetter.TinderboxBuild(buildobj['timestamp'])
    elif buildobj['type'] == 'nightly':
      build = BuildGetter.NightlyBuild(parse_nightly_time(buildobj['for']))
    else:
      raise Exception("Unkown build type %s" % buildobj['type'])

    ret = BatchBuild(build, buildobj['revision'])
    ret.timestamp = buildobj['timestamp']
    ret.note = buildobj['note']
    ret.started = buildobj['started']
    ret.finished = buildobj['finished']

    return ret

  def serialize(self):
    ret = {
      'timestamp' : self.build.get_buildtime(),
      'revision' : self.revision,
      'note' : self.note,
      'started' : self.started,
      'finished' : self.finished
    }

    if isinstance(self.build, BuildGetter.CompileBuild):
      ret['type'] = 'compile'
    elif isinstance(self.build, BuildGetter.TinderboxBuild):
      ret['type'] = 'tinderbox'
    elif isinstance(self.build, BuildGetter.NightlyBuild):
      # Date of nightly might not correspond to build timestamp
      ret['for'] = '%u-%u-%u' % (self.build._date.year, self.build._date.month, self.build._date.day)
      ret['type'] = 'nightly'
    else:
      raise Exception("Unknown build type %s" % (build,))
    return ret

class BatchTest(object):
  def __init__(self, args, out=sys.stdout):
    # See BatchTestCLI for args documentation, for the time being
    self.args = args    
    self.logfile = None
    self.out = out
    self.starttime = time.time()
    self.buildindex = 0
    self.pool = None
    self.builds = {
      'building' : None,
      'prepared': [],
      'running': [],
      'pending': [],
      'skipped': [],
      'completed': [],
      'failed': []
    }
    self.processedbatches = []
    self.pendingbatches = []

    if (self.args.get('hook')):
      sys.path.append(os.path.abspath(os.path.dirname(self.args.get('hook'))))
      self.hook = os.path.basename(self.args.get('hook'))
    else:
      self.hook = None
    
    self.builder = None
    self.builder_mode = None
    self.builder_batch = None
    self.manager = multiprocessing.Manager()
    self.builder_result = self.manager.dict({ 'result': 'not started', 'ret' : None })

  def stat(self, msg=""):
    msg = "%s :: %s\n" % (time.ctime(), msg)
    if self.out:
      self.out.write("[BatchTester] %s" % msg)
    if self.logfile:
      self.logfile.write(msg)
      self.logfile.flush()

  #
  # Resets worker pool
  def reset_pool(self):
    if self.pool:
      self.pool.close()
      self.pool.join()
    self.buildnum = 0
    self.pool = multiprocessing.Pool(processes=self.args['processes'], maxtasksperchild=1)

  #
  # Writes/updates the status file
  def write_status(self):
    statfile = self.args.get('status_file')
    if not statfile: return
    status = {
              'starttime' : self.starttime,
              'building': self.builds['building'].serialize(),
              'batches' : self.processedbatches,
              'pendingbatches' : self.pendingbatches
            }
    for x in self.builds:
      status[x] = filter(lambda y: y.serialize(), self.builds[x])

    tempfile = os.path.join(os.path.dirname(statfile), ".%s" % os.path.basename(statfile))
    sf = open(tempfile, 'w')
    json.dump(status, sf, indent=2)
    if is_win:
      os.remove(statfile) # Can't do atomic renames on windows
    os.rename(tempfile, statfile)
    sf.close()

  # Builds that are in the pending/running list already
  def build_is_queued(build):
    for x in self.running + self.pending:
      if type(x.build) == type(build.build) and x.revision == build.revision:
        return True
    return False
  # Given a set of arguments, lookup & add all specified builds to our queue.
  # This happens asyncrhonously, so not all builds may be queued immediately
  def add_batch(self, batchargs):
    self.pendingbatches.append({ 'args' : batchargs, 'note' : None })

  # Checks on the builder subprocess, getting its result, starting it if needed,
  # etc
  def check_builder(self):
    # Did it exit?
    if self.builder and not self.builder.is_alive():
      self.builder.join()
      self.builder = None

      # Finished a batch queue job
      if self.builder_mode == 'batch':
        if self.builder_result['result'] == 'success':
          if self.builder_batch['args'].get('prioritize'):
            self.builds['pending'] = self.builder_result['ret'][0] + self.builds['pending']
          else:
            self.builds['pending'].extend(self.builder_result['ret'][0])
          self.builds['skipped'].extend(self.builder_result['ret'][1])
        else:
          self.builder_batch['note'] = "Failed: %s" % (self.builder_result['ret'],)
        self.stat("Batch completed: %s (%s)" % (self.builder_batch['args'], self.builder_batch['note']))
        self.processedbatches.append(self.builder_batch)
        self.builder_batch = None

      # Finished a build job
      elif self.builder_mode == 'build':
        build = self.builds['building']
        self.stat("Build %u completed" % (build.num,))
        self.builds['building'] = None
        if self.builder_result['result'] == 'success':
          self.builds['prepared'].append(build)
        else:
          build.note = "Failed: %s" % (self.builder_result['ret'],)
          self.builds['failed'] = build
      self.builder_result['result'] = 'uninitialied'
      self.builder_result['ret'] = None
      self.builder_mode = None
      
    # Should it run?
    if not self.builder and len(self.pendingbatches):
      self.builder_mode = 'batch'
      self.builder_batch = self.pendingbatches.pop()
      self.stat("Handling batch %s" % (self.builder_batch,))
      self.builder = multiprocessing.Process(target=self._process_batch, args=(self.args, self.builder_batch['args'], self.builder_result, self.hook))
      self.builder.start()
    elif not self.builder and self.builds['building']:
      self.builder_mode = 'build'
      self.stat("Starting build for %s :: %s" % (self.builds['building'].num, self.builds['building'].serialize()))
      self.builder = multiprocessing.Process(target=self.prepare_build, args=(self.builds['building'], self.builder_result))
      self.builder.start()

  @staticmethod
  def prepare_build(build, result):
    if build.build.prepare():
      result['result'] = 'success'
    else:
      result['result'] = 'failed'

    result['ret'] = None
    
  #
  # Run loop
  #
  def run(self):
    if not self.args.get('repo'):
      raise Exception('--repo is required for resolving full commit IDs (even on non-compile builds)')

    statfile = self.args.get("status_file")

    if self.args.get('logdir'):
      logfile = open(os.path.join(args.get('logdir'), 'tester.log'), 'a')

    self.stat("Starting at %s with args \"%s\"" % (time.ctime(), sys.argv))

    self.reset_pool()

    batchmode = self.args.get('batch')
    if batchmode:
      if statfile and os.path.exists(statfile) and args.get('status_resume'):
        sf = open(statfile, 'r')
        ostat = json.load(sf)
        sf.close()
        for x in ostat['running'] + ostat['prepared'] + ostat['building'] + ostat['pending']:
          self.pending.append(BatchBuild.deserialize(x))
    else:
      self.add_batch(self.args)

    while True:
      # Clean up finished builds
      for build in self.builds['running']:
        if not build.task.ready(): continue
        
        taskresult = build.task.get() if build.task.successful() else False
        if taskresult is True:
          self.stat("Build %u finished" % (build.num,))
          self.builds['completed'].append(build)
        else:
          self.stat("!! Build %u failed" % (task.num,))
          build.note = "Task returned error %s" % (taskresult,)
          self.builds['failed'].append(build)
        build.finished = time.time()
        self.builds['running'].remove(build)
        build.cleanup()

      # Check on builder
      self.check_builder()

      # Read any pending jobs if we're in batchmode
      while batchmode:
        bcmd = None
        rcmd = get_queued_job(batchmode)
        if not rcmd: break
        try:
          bcmd = vars(parser.parse_args(shlex.split(rcmd)))
        except SystemExit, e: # Don't let argparser actually exit on fail
          note = "Failed to parse batch file command: \"%s\"" % (rcmd,)
          self.stat(note)
          self.processedbatches.append({ 'args' : rcmd, 'note': note })
        if bcmd:
          add_batch(bcmd)

      # Prepare pending builds, but not more than max * 2, as prepared builds
      # takeup space (hundreds of queued builds would fill /tmp with gigabytes
      # of things)
      in_progress = len(self.builds['prepared']) + len(self.builds['pending']) + len(self.builds['running'])
      if len(self.builds['pending']) and not self.builds['building'] and in_progress < self.args['processes'] * 2:
        build = self.builds['pending'][0]
        self.builds['building'] = build
        self.builds['pending'].remove(build)
        build.num = self.buildindex
        self.buildindex += 1

      # Start builds if pool is not filled
      while len(self.builds['prepared']) and len(self.builds['running']) < self.args['processes']:
        build = self.builds['prepared'][0]
        self.builds['prepared'].remove(build)
        build.started = time.time()
        self.stat("Moving build %u to running" % (build.num,))
        for x in vars(build):
          print("%s: %s" % (x, getattr(build, x)))
        build.task = self.pool.apply_async(self.test_build, [build, self.args])
        self.builds['running'].append(build)

      self.write_status()
      
      if not self.builder and not self.builds['building'] and in_progress == 0:
        # out of things to do
        if not batchmode:
          break # Done
        else:
          if self.buildindex > 0:
            self.stat("All tasks complete. Resetting")
            # Reset buildnum when empty to keep it from getting too large
            # (it affects vnc display # and such, which isn't infinite)
            self.reset_pool()
            # Remove items older than 1 day from these lists
            self.builds['completed'] = filter(lambda x: (x.finished + 60 * 60 * 24) > time.time(), self.builds['completed'])
            self.builds['failed'] = filter(lambda x: (x.finished + 60 * 60 * 24) > time.time(), self.builds['failed'])
            self.builds['skipped'] = filter(lambda x: (x.finished + 60 * 60 * 24) > time.time(), self.builds['skipped'])
            batches = filter(lambda x: (x.processed + 60 * 60 * 24) > time.time(), batches)
          else:
            time.sleep(1)
      else:
        # Wait a little and repeat loop
        time.sleep(1)

    self.stat("No more tasks, exiting")
    self.pool.close()
    self.pool.join()
    self.pool = None

  # Threaded call the builder is started on. Calls _process_batch_inner and
  # handles return results
  @staticmethod
  def _process_batch(globalargs, batchargs, returnproxy, hook):
    try:
      if hook:
        mod = __import__(globalargs.get('hook'))
      else:
        mod = None
      ret = BatchTest._process_batch_inner(globalargs, batchargs, mod)
    except Exception, e:
      ret = "An exception occured while processing batch -- %s: %s" % (type(e), e)

    if type(ret) == str:
      returnproxy['result'] = 'error'
    else:
      returnproxy['result'] = 'success'
    returnproxy['ret'] = ret

  #
  # Inner call for _process_batch
  @staticmethod
  def _process_batch_inner(globalargs, batchargs, hook):
    if not batchargs['firstbuild']:
      raise Exception("--firstbuild is required")

    if not globalargs.get('no_pull'):
      # Do a tip lookup to pull the repo so get_full_revision is up to date
      BuildGetter.get_hg_range(globalargs.get('repo'), '.', '.', True)

    mode = batchargs['mode']
    dorange = batchargs['lastbuild']
    builds = []
    # Queue builds
    if mode == 'nightly':
      startdate = parse_nightly_time(batchargs['firstbuild'])
      if dorange:
        enddate = parse_nightly_time(batchargs['lastbuild'])
        dates = range(startdate.toordinal(), enddate.toordinal() + 1)
      else:
        dates = [ startdate.toordinal() ]
      for x in dates:
        builds.append(BuildGetter.NightlyBuild(datetime.date.fromordinal(x)))
    elif mode == 'tinderbox':
      startdate = float(batchargs['firstbuild'])
      if dorange:
        enddate = float(batchargs['lastbuild'])
        builds.extend(BuildGetter.get_tinderbox_builds(startdate, enddate))
      else:
        builds.append(BuildGetter.TinderboxBuild(startdate))
    elif mode == 'build':
      repo = batchargs.get('repo') if batchargs.get('repo') else globalargs.get('repo')
      objdir = batchargs.get('objdir') if batchargs.get('objdir') else globalargs.get('objdir')
      mozconfig = batchargs.get('mozconfig') if batchargs.get('mozconfig') else globalargs.get('mozconfig')
      if not repo or not mozconfig or not objdir:
        raise Exception("Build mode requires --repo, --mozconfig, and --objdir to be set")
      if dorange:
        lastbuild = batchargs['lastbuild']
      else:
        lastbuild = batchargs['firstbuild']
      for commit in BuildGetter.get_hg_range(repo, batchargs['firstbuild'], lastbuild, not globalargs.get("no_pull")):
        if globalargs.get('logdir'):
          logfile = os.path.join(globalargs.get('logdir'), "%s.build.log" % (commit,))
        else:
          logfile = None
        builds.append(BuildGetter.CompileBuild(repo, mozconfig, objdir, pull=True, commit=commit, log=logfile))
    else:
      raise Exception("Unknown mode %s" % mode)

    readybuilds = []
    skippedbuilds = []
    for build in builds:
      revision = build.get_revision()
      if len(revision) < 40:
        # We want the full revision ID for database
        revinfo = BuildGetter.get_hg_range(globalargs.get("repo"), revision, revision)
        if revinfo and len(revinfo):
          revision = revinfo[0]
        else:
          revision = None

      build = BatchBuild(build, revision)
      if not revision:
        build.note = "Failed to lookup full revision"
      elif hook and not hook.should_test(build, globalargs):
        build.note = "Build skipped by tester (likely already tested)";
      else:
        readybuilds.append(build)
        break

      build.finished = time.time()
      skippedbuilds.append(build)

    return [ readybuilds, skippedbuilds ]

  #
  # Build testing pool
  #
  @staticmethod
  def test_build(build, globalargs, hook=None):
    mod = None
    if not hook:
      return "Cannot test builds without a --hook providing run_tests(Build)"

    try:
      mod = __import__(hook)
      # TODO BenchTester should actually dynamically pick a free port, rather than
      # taking it as a parameter.
      s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      try:
        s.bind(('', 24242 + buildindex))
      except Exception, e:
        raise Exception("Test error: jsbridge port %u unavailable" % (24242 + buildindex,))
      s.close()

      mod.run_tests(build, globalargs)
    except (Exception, KeyboardInterrupt) as e:
      err = "Test worker encountered an exception:\n%s :: %s" % (type(e), e)
      ret = err
    return ret

class BatchTestCLI(BatchTest):
  def __init__(self, args=sys.argv[1:]):
    self.parser = argparse.ArgumentParser(description='Run tests against one or more builds in parallel')
    self.parser.add_argument('--mode', help='nightly or tinderbox or build')
    self.parser.add_argument('--batch', help='Batch mode -- given a folder name, treat each file within as containing a set of arguments to this script, deleting each file as it is processed.')
    self.parser.add_argument('--firstbuild', help='For nightly, the date (YYYY-MM-DD) of the first build to test. For tinderbox, the timestamp to start testing builds at. For build, the first revision to build.')
    self.parser.add_argument('--lastbuild', help='[optional] For nightly builds, the last date to test. For tinderbox, the timestamp to stop testing builds at. For build, the last revision to build If omitted, first_build is the only build tested.')
    self.parser.add_argument('-p', '--processes', help='Number of tests to run in parallel.', default=1, type=int)
    self.parser.add_argument('--hook', help='Name of a python file to import for each test. The test will call should_test(BatchBuild), run_tests(BatchBuild), and cli_hook(argparser) in this file.')
    self.parser.add_argument('--logdir', '-l', help="Directory to log progress to. Doesn't make sense for batched processes. Creates 'tester.log', 'buildname.test.log' and 'buildname.build.log' (for compile builds).")
    self.parser.add_argument('--repo', help="For build mode, the checked out FF repo to use")
    self.parser.add_argument('--mozconfig', help="For build mode, the mozconfig to use")
    self.parser.add_argument('--objdir', help="For build mode, the objdir provided mozconfig will create")
    self.parser.add_argument('--no-pull', action='store_true', help="For build mode, don't run a hg pull in the repo before messing with a commit")
    self.parser.add_argument('--status-file', help="A file to keep a json-dump of the currently running job status in. This file is mv'd into place to avoid read/write issues")
    self.parser.add_argument('--status-resume', action='store_true', help="Resume any jobs still present in the status file. Useful for interrupted sessions")
    self.parser.add_argument('--prioritize', action='store_true', help="For batch'd builds, insert at the beginning of the pending queue rather than the end")
    temp = vars(self.parser.parse_known_args(args)[0])
    if temp.get('hook'):
      sys.path.append(os.path.abspath(os.path.dirname(temp.get('hook'))))
      mod = __import__(os.path.basename(temp.get('hook')))
      mod.cli_hook(self.parser)

    args = vars(self.parser.parse_args(args))
    super(BatchTestCLI, self).__init__(args)

#
# Main
#

if __name__ == '__main__':
  cli = BatchTestCLI()
  cli.run()