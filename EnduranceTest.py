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
    self.name = "EnduranceTest"
  
  def setup(self):
    self.info("Setting up Endurance module")
    self.ready = True
    return True
  
  def endurance_event(self, obj):
    if obj['iterations']:
      self.info("Got enduranceResults callback")
      self.endurance_results = obj
    else:
      self.error("Got endurance test result with 0 iterations: %s" % obj)
  
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
    self.info("Mozmill - setting up")
    mozmillinst = mozmill.MozMill()
    mozmillinst.persisted['endurance'] = testvars
    mozmillinst.add_listener(self.endurance_event, eventType='mozmill.enduranceResults')
    
    profile = mozrunner.FirefoxProfile(binary=self.tester.binary,
                                       profile=profdir,
                                       addons=[jsbridge.extension_path, mozmill.extension_path],
                                       # Don't open the first-run dialog, it loads a video
                                       # and other things that can screw with benchmarks
                                       preferences={'startup.homepage_welcome_url' : '',
                                                    'startup.homepage_override_url' :''})
    runner = mozrunner.FirefoxRunner(binary=self.tester.binary, profile=profile)
    
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
    except Exception, e:
      return self.error("Endurance test run failed")
    
    self.info("Endurance - cleaning up")
    # HACK
    # jsbridge doesn't cleanup its polling thread properly,
    # resulting in a 100% CPU thread being spawned for every
    # time we re-create mozmill.
    import thread
    try:
      mozmillinst.bridge.handle_close = thread.exit
      mozmillinst.back_channel.handle_close = thread.exit
      mozmillinst.stop()
    except Exception, e:
      self.error("Failed to properly cleanup mozmill")
    
    shutil.rmtree(profdir)
      
    self.info("Endurance - saving results")
    
    if not self.endurance_results:
      return self.error("Test did not return any endurance data!")
      
    results = {}
    for iternum in range(len(self.endurance_results['iterations'])):
      iteration = self.endurance_results['iterations'][iternum]
      for checkpoint in iteration['checkpoints']:
        # Get rid of the [i:0, e:5] crap endurance adds
        label = re.sub(" \[i:\d+ e:\d+\]$", "", checkpoint['label'])
        for memtype,memval in checkpoint['memory'].items():
          results[".".join(["Iteration %u" % iternum, label, "mem", memtype])] = memval
    
    if not self.tester.add_test_results(testname, results):
      return self.error("Failed to save test results")
    self.info("Test '%s' complete" % testname)
    return True
