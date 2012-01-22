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

import os
import re
import ConfigParser

from devstack import env
from devstack import exceptions as excp
from devstack import log as logging
from devstack import shell as sh
from devstack import utils

LOG = logging.getLogger("devstack.cfg")
PW_TMPL = "Enter a password for %s: "
ENV_PAT = re.compile(r"^\s*\$\{([\w\d]+):\-(.*)\}\s*$")
SUB_MATCH = re.compile(r"(?:([\w\d]+):([\w\d]+))")
EVAL_MATCH = re.compile(r"\$\((.*)\)")
CACHE_MSG = "(value will now be internally cached)"


class IgnoreMissingConfigParser(ConfigParser.RawConfigParser):
    DEF_INT = 0
    DEF_FLOAT = 0.0
    DEF_BOOLEAN = False

    def __init__(self):
        ConfigParser.RawConfigParser.__init__(self, allow_no_value=True)

    def get(self, section, option):
        value = None
        try:
            value = ConfigParser.RawConfigParser.get(self, section, option)
        except ConfigParser.NoSectionError, e:
            pass
        except ConfigParser.NoOptionError, e:
            pass
        return value

    def getboolean(self, section, option):
        value = self.get(section, option)
        if(value == None):
            #not there so don't let the parent blowup
            return IgnoreMissingConfigParser.DEF_BOOLEAN
        return ConfigParser.RawConfigParser.getboolean(self, section, option)

    def getfloat(self, section, option):
        value = self.get(section, option)
        if(value == None):
            #not there so don't let the parent blowup
            return IgnoreMissingConfigParser.DEF_FLOAT
        return ConfigParser.RawConfigParser.getfloat(self, section, option)

    def getint(self, section, option):
        value = self.get(section, option)
        if(value == None):
            #not there so don't let the parent blowup
            return IgnoreMissingConfigParser.DEF_INT
        return ConfigParser.RawConfigParser.getint(self, section, option)


class EnvConfigParser(ConfigParser.RawConfigParser):
    def __init__(self):
        ConfigParser.RawConfigParser.__init__(self, allow_no_value=True)
        self.pws = dict()
        self.configs_fetched = dict()
        self.db_dsns = dict()

    def _makekey(self, section, option):
        parts = [section, option]
        return "/".join(parts)

    def get(self, section, option):
        key = self._makekey(section, option)
        v = None
        if(key in self.configs_fetched):
            v = self.configs_fetched.get(key)
            LOG.debug("Fetched cached value \"%s\" for param \"%s\"" % (v, key))
        else:
            LOG.debug("Fetching value for param \"%s\"" % (key))
            v = self._get_special(section, option)
            LOG.debug("Fetched \"%s\" for \"%s\"" % (v, key))
            self.configs_fetched[key] = v
        return v

    def _eval_expr(self, expr):
        LOG.debug("Evaluating expression %s", expr)
        if(SUB_MATCH.search(expr)):
            def replacer(match):
                section = match.group(1).strip()
                option = match.group(2).strip()
                #recursion may happen alot here
                #but you can shot yourself if u want to
                return self.get(section, option)
            full_expr = SUB_MATCH.sub(replacer, expr)
            LOG.debug("Evaluating complete expression %s", full_expr)
            expr = full_expr
        #this isn't the safest, but we aren't
        #expecting crazy user inputs...
        local_vars = dict()
        global_vars = dict()
        eval_result = eval(expr, global_vars, local_vars)
        return str(eval_result)

    def _extract_default(self, value):
        eval_mtch = EVAL_MATCH.match(value)
        if(not eval_mtch):
            return value
        expr = eval_mtch.group(1)
        if(len(expr) == 0):
            return value
        return self._eval_expr(expr)

    def _get_special(self, section, option):
        key = self._makekey(section, option)
        parent_val = ConfigParser.RawConfigParser.get(self, section, option)
        if(parent_val == None):
            return None
        extracted_val = None
        mtch = ENV_PAT.match(parent_val)
        if(mtch):
            env_key = mtch.group(1).strip()
            def_val = mtch.group(2)
            if(len(def_val) == 0 and len(env_key) == 0):
                msg = "Invalid bash-like value \"%s\" for \"%s\"" % (parent_val, key)
                raise excp.BadParamException(msg)
            if(len(env_key) == 0 or env.get_key(env_key) == None):
                LOG.debug("Extracting default value from config provided default value \"%s\" for \"%s\"" % (def_val, key))
                actual_def_val = self._extract_default(def_val)
                LOG.debug("Using config provided default value \"%s\" for \"%s\" (no environment key)" % (actual_def_val, key))
                extracted_val = actual_def_val
            else:
                env_val = env.get_key(env_key)
                LOG.debug("Using enviroment provided value \"%s\" for \"%s\"" % (env_val, key))
                extracted_val = env_val
        else:
            LOG.debug("Using raw config provided value \"%s\" for \"%s\"" % (parent_val, key))
            extracted_val = parent_val
        return extracted_val

    def get_host_ip(self):
        host_ip = self.get('default', 'host_ip')
        if(not host_ip):
            LOG.debug("Host ip from configuration/environment was empty, programatically attempting to determine it.")
            host_ip = utils.get_host_ip()
        key = self._makekey('default', 'host_ip')
        self.configs_fetched[key] = host_ip
        LOG.debug("Determined host ip to be: \"%s\"" % (host_ip))
        return host_ip

    def get_dbdsn(self, dbname):
        user = self.get("db", "sql_user")
        host = self.get("db", "sql_host")
        port = self.get("db", "port")
        pw = self.getpw("passwords", "sql")
        #check the dsn cache
        if(dbname in self.db_dsns):
            return self.db_dsns[dbname]
        #form the dsn (from components we have...)
        #dsn = "<driver>://<username>:<password>@<host>:<port>/<database>"
        if(not host):
            msg = "Unable to fetch a database dsn - no host found"
            raise excp.BadParamException(msg)
        driver = self.get("db", "type")
        if(not driver):
            msg = "Unable to fetch a database dsn - no driver type found"
            raise excp.BadParamException(msg)
        dsn = driver + "://"
        if(user):
            dsn += user
        if(pw):
            dsn += ":" + pw
        if(user or pw):
            dsn += "@"
        dsn += host
        if(port):
            dsn += ":" + port
        if(dbname):
            dsn += "/" + dbname
        else:
            dsn += "/"
        LOG.debug("For database \"%s\" fetched dsn \"%s\" %s" % (dbname, dsn, CACHE_MSG))
        #store for later...
        self.db_dsns[dbname] = dsn
        return dsn

    def getpw(self, section, option):
        key = self._makekey(section, option)
        pw = self.pws.get(key)
        if(pw != None):
            return pw
        pw = self.get(section, option)
        if(pw == None):
            pw = ""
        if(len(pw) == 0):
            LOG.debug("Being forced to ask for password for \"%s\" since the configuration/environment value is empty.", key)
            while(len(pw) == 0):
                pw = sh.password(PW_TMPL % (key))
        LOG.debug("Password for \"%s\" will be \"%s\" %s" % (key, pw, CACHE_MSG))
        self.pws[key] = pw
        return pw
