#!/usr/bin/env python

# This Source Code is subject to the terms of the Mozilla Public License
# version 2.0 (the "License"). You can obtain a copy of the License at
# http://mozilla.org/MPL/2.0/.

import BenchTester
import os
import sys
import shutil
import tempfile
import re

class EnduranceTest(BenchTester.BenchTest):
  def __init__(self, parent):
    BenchTester.BenchTest.__init__(self, parent)
    parent.add_argument('--jsbridge_port', help="Port to use for jsbridge, so concurrent tests don't collide")
    self.name = "EnduranceTest"
    self.parent = parent
  
  def setup(self):
    self.info("Setting up Endurance module")
    self.ready = True
    self.endurance_results = None
    
    if 'jsbridge_port' in self.parent.args:
      self.jsport = int(self.parent.args['jsbridge_port'])
    else:
      self.jsport = 24242
    return True
  
  def endurance_event(self, obj):
    if obj['iterations']:
      self.info("Got enduranceResults callback")
      self.endurance_results = obj
    else:
      self.error("Got endurance test result with 0 iterations: %s" % obj)
  
  def endurance_checkpoint(self, obj):
    if obj['checkpoints']:
      self.info("Got enduranceCheckpoint callback")
      if not self.endurance_results:
        self.endurance_results = { 'iterations': [] }
      self.endurance_results['iterations'].append(obj)
    else:
      self.error("Got endurance checkpoint with no data: %s" % obj)
      
  def run_test(self, testname, testvars={}):
    if not self.ready:
      return self.error("run_test() called before setup")
    
    self.info("Beginning endurance test '%s'" % testname)
    
    import mozmill
    import mozrunner
    import jsbridge
    
    profdir = tempfile.mkdtemp("slimtest_profile")
    self.info("Using temporary profile %s" % profdir)
    #
    # Setup mozmill
    #
    self.info("Mozmill - setting up. Using jsbridge port %u" % (self.jsport,))
    mozmillinst = mozmill.MozMill(jsbridge_port=self.jsport)
    mozmillinst.persisted['endurance'] = testvars
    mozmillinst.add_listener(self.endurance_event, eventType='mozmill.enduranceResults')
    # enduranceCheckpoint is used in slimtest's endurance version
    # to avoid keeping everything in the runtime (it records a lot of numbers,
    # which in turn inflate memory usage, which it's trying to measure)
    mozmillinst.add_listener(self.endurance_checkpoint, eventType='mozmill.enduranceCheckpoint')
    
    profile = mozrunner.FirefoxProfile(binary=self.tester.binary,
                                       profile=profdir,
                                       addons=[jsbridge.extension_path, mozmill.extension_path],
                                       # Don't open the first-run dialog, it loads a video
                                       # and other things that can screw with benchmarks
                                       preferences={'startup.homepage_welcome_url' : '',
                                                    'startup.homepage_override_url' :''})
    runner = mozrunner.FirefoxRunner(binary=self.tester.binary, profile=profile)
    runner.cmdargs += ['-jsbridge', str(self.jsport)]
    
    # Add test
    testpath = os.path.join(*testvars['test'])
    if not os.path.exists(testpath):
      return self.error("Test '%s' specifies a test that doesn't exist: %s" % (testname, testpath))
    mozmillinst.tests = [ testpath ]
    
    # Run test
    self.endurance_results = None
    self.info("Endurance - starting browser")
    try:
      mozmillinst.start(profile=runner.profile, runner=runner)
      self.info("Endurance - running test")
      mozmillinst.run_tests(mozmillinst.tests)
      successful = len(mozmillinst.fails) == 0
    except Exception, e:
      try:
        mozmillinst.stop()
        shutil.rmtree(profdir)
      except: pass
      return self.error("Endurance test run failed")
    
    self.info("Endurance - cleaning up")
    try:
      mozmillinst.stop()
    except Exception, e:
      self.error("Failed to properly cleanup mozmill")
    
    shutil.rmtree(profdir)
      
    self.info("Endurance - saving results")
    
    if not self.endurance_results:
      return self.error("Test did not return any endurance data!")
      
    results = {}
    for x in range(len(self.endurance_results['iterations'])):
      iteration = self.endurance_results['iterations'][x]
      for checkpoint in iteration['checkpoints']:
        # Endurance adds [i:0, e:5]
        # Because iterations might not be in order when
        # passed from enduranceCheckpoint, parse this.
        label_re = re.match("^(.+) \[i:(\d+) e:\d+\]$", checkpoint['label'])
        if not label_re:
          self.error("Checkpoint '%s' doesn't look like an endurance checkpoint!" % checkpoint['label'])
          next
        iternum = int(label_re.group(2))
        label = label_re.group(1)
        for memtype,memval in checkpoint['memory'].items():
          results["/".join(["Iteration %u" % iternum, label, memtype])] = memval
    
    if not self.tester.add_test_results(testname, results, successful):
      return self.error("Failed to save test results")
    self.info("Test '%s' complete" % testname)
    return True
