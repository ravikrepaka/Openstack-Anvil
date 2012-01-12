# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import os.path

import Logger
import Component
import Shell
import Util
import Trace
from Trace import (TraceWriter, TraceReader)
import Downloader
import Packager
import Runner
import runners.Foreground as Foreground
from runners.Foreground import (ForegroundRunner)
from Util import (GLANCE,
                  get_pkg_list,
                  param_replace, get_dbdsn,
                  )
from Shell import (execute, deldir, mkdirslist, unlink,
                   joinpths, load_file, write_file, touch_file)
import Exceptions
from Exceptions import (StopException, StartException, InstallException)

LOG = Logger.getLogger("install.glance")

#naming + config files
TYPE = GLANCE
API_CONF = "glance-api.conf"
REG_CONF = "glance-registry.conf"
CONFIGS = [API_CONF, REG_CONF]
DB_NAME = "glance"

#why doesn't --record do anything??
PY_INSTALL = ['python', 'setup.py', 'develop']
PY_UNINSTALL = ['python', 'setup.py', 'develop', '--uninstall']

#what to start
APPS_TO_START = ['glance-api', 'glance-registry']
APP_OPTIONS = {
    'glance-api': ['--config-file', joinpths('%ROOT%', "etc", API_CONF)],
    'glance-registry': ['--config-file', joinpths('%ROOT%', "etc", REG_CONF)]
}


class GlanceBase(Component.ComponentBase):
    def __init__(self, *args, **kargs):
        Component.ComponentBase.__init__(self, TYPE, *args, **kargs)
        #note not config
        self.cfgdir = joinpths(self.appdir, "etc")


class GlanceUninstaller(GlanceBase, Component.UninstallComponent):
    def __init__(self, *args, **kargs):
        GlanceBase.__init__(self, *args, **kargs)
        self.tracereader = TraceReader(self.tracedir, Trace.IN_TRACE)

    def unconfigure(self):
        #get rid of all files configured
        cfgfiles = self.tracereader.files_configured()
        if(len(cfgfiles)):
            am = len(cfgfiles)
            LOG.info("Removing %s configuration files" % (am))
            for fn in cfgfiles:
                if(len(fn)):
                    unlink(fn)
                    LOG.info("Removed %s" % (fn))

    def uninstall(self):
        #clean out removeable packages
        pkgsfull = self.tracereader.packages_installed()
        if(len(pkgsfull)):
            am = len(pkgsfull)
            LOG.info("Removing %s packages" % (am))
            self.packager.remove_batch(pkgsfull)
        #clean out files touched
        filestouched = self.tracereader.files_touched()
        if(len(filestouched)):
            am = len(pkgsfull)
            LOG.info("Removing %s touched files" % (am))
            for fn in filestouched:
                if(len(fn)):
                    unlink(fn)
                    LOG.info("Removed %s" % (fn))
        #undevelop python???
        #how should this be done??
        pylisting = self.tracereader.py_listing()
        if(pylisting != None):
            execute(*PY_UNINSTALL, cwd=self.appdir, run_as_root=True)
        #clean out dirs created
        dirsmade = self.tracereader.dirs_made()
        if(len(dirsmade)):
            am = len(dirsmade)
            LOG.info("Removing %s created directories" % (am))
            for dirname in dirsmade:
                deldir(dirname)
                LOG.info("Removed %s" % (dirname))


class GlanceRuntime(GlanceBase, Component.RuntimeComponent):
    def __init__(self, *args, **kargs):
        GlanceBase.__init__(self, *args, **kargs)
        self.foreground = kargs.get("foreground", True)
        self.tracereader = TraceReader(self.tracedir, Trace.IN_TRACE)
        self.tracewriter = TraceWriter(self.tracedir, Trace.START_TRACE)
        self.starttracereader = TraceReader(self.tracedir, Trace.START_TRACE)

    def start(self):
        #ensure it was installed
        pylisting = self.tracereader.py_listing()
        if(len(pylisting) == 0):
            msg = "Can not start %s since it was not installed" % (TYPE)
            raise StartException(msg)
        #select how we are going to start i
        if(self.foreground):
            starter = ForegroundRunner()
        else:
            raise NotImplementedError("Can not yet start in screen mode")
        #start all apps
        fns = list()
        replacements = dict()
        replacements['ROOT'] = self.appdir
        for app in APPS_TO_START:
            #adjust the program options now that we have real locations
            program_opts = []
            for opt in APP_OPTIONS.get(app):
                program_opts.append(param_replace(opt, replacements))
            LOG.info("Starting %s with options [%s]" % (app, ", ".join(program_opts)))
            #start it with the given settings
            fn = starter.start(app, app, *program_opts,
                app_dir=self.appdir, trace_dir=self.tracedir)
            if(fn):
                fns.append(fn)
                LOG.info("Started %s, details are in %s" % (app, fn))
                # This trace is used to locate details about what to stop
                self.tracewriter.started_info(app, fn)
            else:
                LOG.info("Started %s" % (app))
        return fns

    def stop(self):
        #ensure it was installed
        pylisting = self.tracereader.py_listing()
        if(pylisting == None or len(pylisting) == 0):
            msg = "Can not start %s since it was not installed" % (TYPE)
            raise StopException(msg)
        #we can only stop what has a started trace
        start_traces = self.starttracereader.apps_started()
        killedam = 0
        for mp in start_traces:
            #extract the apps name and where its trace is
            fn = mp.get('trace_fn')
            name = mp.get('name')
            #missing some key info, skip it
            if(fn == None or name == None):
                continue
            #figure out which class will stop it
            contents = Trace.parse_fn(fn)
            runtype = None
            for (cmd, action) in contents:
                if(cmd == Runner.RUN_TYPE and action == Foreground.RUN_TYPE):
                    runtype = ForegroundRunner
                    break
            #we can try to stop it
            if(runtype != None):
                LOG.info("Stopping %s with %s in %s" % (name, runtype, self.tracedir))
                killer = runtype()
                killer.stop(name, trace_dir=self.tracedir)
                killedam += 1
        #if we got rid of them all get rid of the trace
        if(killedam == len(start_traces)):
            fn = self.starttracereader.trace_fn
            LOG.info("Deleting trace file %s" % (fn))
            unlink(fn)


class GlanceInstaller(GlanceBase, Component.InstallComponent):
    def __init__(self, *args, **kargs):
        GlanceBase.__init__(self, *args, **kargs)
        self.gitloc = self.cfg.get("git", "glance_repo")
        self.brch = self.cfg.get("git", "glance_branch")
        self.tracewriter = TraceWriter(self.tracedir, Trace.IN_TRACE)

    def download(self):
        dirsmade = Downloader.download(self.appdir, self.gitloc, self.brch)
        # This trace isn't used yet but could be
        self.tracewriter.downloaded(self.appdir, self.gitloc)
        # This trace is used to remove the dirs created
        self.tracewriter.dir_made(*dirsmade)
        return self.tracedir

    def install(self):
        #get all the packages for glance for the specified distro
        pkgs = get_pkg_list(self.distro, TYPE)
        pkgnames = sorted(pkgs.keys())
        LOG.debug("Installing packages %s" % (", ".join(pkgnames)))
        Packager.pre_install(pkgs, self._get_param_map())
        self.packager.install_batch(pkgs)
        Packager.post_install(pkgs, self._get_param_map())
        for name in pkgnames:
            packageinfo = pkgs.get(name)
            version = packageinfo.get("version", "")
            remove = packageinfo.get("removable", True)
            # This trace is used to remove the pkgs
            self.tracewriter.package_install(name, remove, version)
        #make a directory for the python trace file (if its not already there)
        dirsmade = mkdirslist(self.tracedir)
        # This trace is used to remove the dirs created
        self.tracewriter.dir_made(*dirsmade)
        recordwhere = Trace.touch_trace(self.tracedir, Trace.PY_TRACE)
        # This trace is used to remove the trace created
        self.tracewriter.py_install(recordwhere)
        (sysout, stderr) = execute(*PY_INSTALL, cwd=self.appdir, run_as_root=True)
        write_file(recordwhere, sysout)
        return self.tracedir

    def configure(self):
        dirsmade = mkdirslist(self.cfgdir)
        # This trace is used to remove the dirs created
        self.tracewriter.dir_made(*dirsmade)
        parameters = self._get_param_map()
        for fn in CONFIGS:
            #go through each config in devstack (which is really a template)
            #and adjust that template to have real values and then go through
            #the resultant config file and perform and adjustments (directory creation...)
            #and then write that to the glance configuration directory.
            sourcefn = joinpths(Util.STACK_CONFIG_DIR, TYPE, fn)
            tgtfn = joinpths(self.cfgdir, fn)
            LOG.info("Configuring template file %s" % (sourcefn))
            contents = load_file(sourcefn)
            LOG.info("Replacing parameters in file %s" % (sourcefn))
            LOG.debug("Replacements = %s" % (parameters))
            contents = param_replace(contents, parameters)
            LOG.debug("Applying side-effects of param replacement for template %s" % (sourcefn))
            self._config_apply(contents, fn)
            LOG.info("Writing configuration file %s" % (tgtfn))
            write_file(tgtfn, contents)
            # This trace is used to remove the files configured
            self.tracewriter.cfg_write(tgtfn)
        return self.tracedir

    def _config_apply(self, contents, fn):
        lines = contents.splitlines()
        for line in lines:
            cleaned = line.strip()
            if(len(cleaned) == 0 or cleaned[0] == '#' or cleaned[0] == '['):
                #not useful to examine these
                continue
            pieces = cleaned.split("=", 1)
            if(len(pieces) != 2):
                continue
            key = pieces[0].strip()
            val = pieces[1].strip()
            if(len(key) == 0 or len(val) == 0):
                continue
            #now we take special actions
            if(key == 'filesystem_store_datadir'):
                # Delete existing images
                deldir(val)
                # Recreate
                dirsmade = mkdirslist(val)
                self.tracewriter.dir_made(*dirsmade)
            elif(key == 'log_file'):
                # Ensure that we can write to the log file
                dirname = os.path.dirname(val)
                if(len(dirname)):
                    dirsmade = mkdirslist(dirname)
                    # This trace is used to remove the dirs created
                    self.tracewriter.dir_made(*dirsmade)
                # Destroy then recreate it
                unlink(val)
                touch_file(val)
                self.tracewriter.file_touched(val)
            elif(key == 'image_cache_datadir'):
                # Destroy then recreate it
                deldir(val)
                dirsmade = mkdirslist(val)
                # This trace is used to remove the dirs created
                self.tracewriter.dir_made(*dirsmade)
            elif(key == 'scrubber_datadir'):
                # Destroy then recreate it
                deldir(val)
                dirsmade = mkdirslist(val)
                # This trace is used to remove the dirs created
                self.tracewriter.dir_made(*dirsmade)

    def _get_param_map(self):
        # These be used to fill in the configuration
        # params with actual values
        mp = dict()
        mp['DEST'] = self.appdir
        mp['SYSLOG'] = self.cfg.getboolean("default", "syslog")
        mp['SERVICE_TOKEN'] = self.cfg.getpw("passwords", "service_token")
        mp['SQL_CONN'] = get_dbdsn(self.cfg, DB_NAME)
        return mp
