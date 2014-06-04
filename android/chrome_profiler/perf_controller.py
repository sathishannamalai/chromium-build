# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
import os
import subprocess
import sys
import tempfile

from chrome_profiler import controllers

from pylib import android_commands
from pylib import constants

sys.path.append(os.path.join(constants.DIR_SOURCE_ROOT,
                             'tools',
                             'telemetry'))
try:
  # pylint: disable=F0401
  from telemetry.core.platform.profiler import android_profiling_helper
  from telemetry.util import support_binaries
except ImportError:
  android_profiling_helper = None
  support_binaries = None


_PERF_OPTIONS = [
    # Sample across all processes and CPUs to so that the current CPU gets
    # recorded to each sample.
    '--all-cpus',
    # In perf 3.13 --call-graph requires an argument, so use the -g short-hand
    # which does not.
    '-g',
    # Increase priority to avoid dropping samples. Requires root.
    '--realtime', '80',
    # Record raw samples to get CPU information.
    '--raw-samples',
    # Increase sampling frequency for better coverage.
    '--freq', '2000',
]


class _PerfProfiler(object):
  def __init__(self, device, perf_binary, categories):
    self._device = device
    self._output_file = android_commands.DeviceTempFile(
        self._device.old_interface, prefix='perf_output')
    self._log_file = tempfile.TemporaryFile()

    device_param = (['-s', self._device.old_interface.GetDevice()]
                    if self._device.old_interface.GetDevice() else [])
    cmd = ['adb'] + device_param + \
          ['shell', perf_binary, 'record',
           '--output', self._output_file.name] + _PERF_OPTIONS
    if categories:
      cmd += ['--event', ','.join(categories)]
    self._perf_process = subprocess.Popen(cmd,
                                          stdout=self._log_file,
                                          stderr=subprocess.STDOUT)

  def SignalAndWait(self):
    perf_pids = self._device.old_interface.ExtractPid('perf')
    self._device.old_interface.RunShellCommand(
        'kill -SIGINT ' + ' '.join(perf_pids))
    self._perf_process.wait()

  def _FailWithLog(self, msg):
    self._log_file.seek(0)
    log = self._log_file.read()
    raise RuntimeError('%s. Log output:\n%s' % (msg, log))

  def PullResult(self, output_path):
    if not self._device.old_interface.FileExistsOnDevice(
        self._output_file.name):
      self._FailWithLog('Perf recorded no data')

    perf_profile = os.path.join(output_path,
                                os.path.basename(self._output_file.name))
    self._device.old_interface.PullFileFromDevice(self._output_file.name,
                                                  perf_profile)
    if not os.stat(perf_profile).st_size:
      os.remove(perf_profile)
      self._FailWithLog('Perf recorded a zero-sized file')

    self._log_file.close()
    self._output_file.close()
    return perf_profile


class PerfProfilerController(controllers.BaseController):
  def __init__(self, device, categories):
    controllers.BaseController.__init__(self)
    self._device = device
    self._categories = categories
    self._perf_binary = self._PrepareDevice(device)
    self._perf_instance = None

  def __repr__(self):
    return 'perf profile'

  @staticmethod
  def IsSupported():
    return bool(android_profiling_helper)

  @staticmethod
  def _PrepareDevice(device):
    if not 'BUILDTYPE' in os.environ:
      os.environ['BUILDTYPE'] = 'Release'
    return android_profiling_helper.PrepareDeviceForPerf(device)

  @classmethod
  def GetCategories(cls, device):
    perf_binary = cls._PrepareDevice(device)
    return device.old_interface.RunShellCommand('%s list' % perf_binary)

  def StartTracing(self, _):
    self._perf_instance = _PerfProfiler(self._device,
                                        self._perf_binary,
                                        self._categories)

  def StopTracing(self):
    if not self._perf_instance:
      return
    self._perf_instance.SignalAndWait()

  def PullTrace(self):
    symfs_dir = os.path.join(tempfile.gettempdir(),
                             os.path.expandvars('$USER-perf-symfs'))
    if not os.path.exists(symfs_dir):
      os.makedirs(symfs_dir)
    required_libs = set()

    # Download the recorded perf profile.
    perf_profile = self._perf_instance.PullResult(symfs_dir)
    required_libs = \
        android_profiling_helper.GetRequiredLibrariesForPerfProfile(
            perf_profile)
    if not required_libs:
      logging.warning('No libraries required by perf trace. Most likely there '
                      'are no samples in the trace.')

    # Build a symfs with all the necessary libraries.
    kallsyms = android_profiling_helper.CreateSymFs(self._device,
                                                    symfs_dir,
                                                    required_libs,
                                                    use_symlinks=False)
    # Convert the perf profile into JSON.
    perfhost_path = os.path.abspath(support_binaries.FindPath(
        'perfhost', 'linux'))
    perf_script_path = os.path.join(constants.DIR_SOURCE_ROOT,
        'tools', 'telemetry', 'telemetry', 'core', 'platform', 'profiler',
        'perf_vis', 'perf_to_tracing.py')
    json_file_name = os.path.basename(perf_profile)
    with open(os.devnull, 'w') as dev_null, \
        open(json_file_name, 'w') as json_file:
      cmd = [perfhost_path, 'script', '-s', perf_script_path, '-i',
             perf_profile, '--symfs', symfs_dir, '--kallsyms', kallsyms]
      subprocess.call(cmd, stdout=json_file, stderr=dev_null)
    return json_file_name