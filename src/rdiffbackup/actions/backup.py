# Copyright 2021 the rdiff-backup project
#
# This file is part of rdiff-backup.
#
# rdiff-backup is free software; you can redistribute it and/or modify
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# rdiff-backup is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rdiff-backup; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA

"""
A built-in rdiff-backup action plug-in to backup a source to a target directory.
"""

import time

from rdiff_backup import (
    backup,
    Globals,
    log,
    Security,
    selection,
    SetConnections,
    Time,
)
from rdiffbackup import actions
from rdiffbackup.locations import (directory, repository)


class BackupAction(actions.BaseAction):
    """
    Backup a source directory to a target backup repository.
    """
    name = "backup"
    security = "backup"
    parent_parsers = [
        actions.CREATION_PARSER, actions.COMPRESSION_PARSER,
        actions.SELECTION_PARSER, actions.FILESYSTEM_PARSER,
        actions.USER_GROUP_PARSER, actions.STATISTICS_PARSER,
    ]

    @classmethod
    def add_action_subparser(cls, sub_handler):
        subparser = super().add_action_subparser(sub_handler)
        subparser.add_argument(
            "locations", metavar="[[USER@]SERVER::]PATH", nargs=2,
            help="locations of SOURCE_DIR and to which REPOSITORY to backup")
        return subparser

    def connect(self):
        conn_value = super().connect()
        if conn_value:
            self.source = directory.ReadDir(self.connected_locations[0],
                                            self.values.force)
            self.target = repository.Repo(
                self.connected_locations[1], self.values.force,
                must_be_writable=True, must_exist=False,
                create_full_path=self.values.create_full_path
            )
        return conn_value

    def check(self):
        # we try to identify as many potential errors as possible before we
        # return, so we gather all potential issues and return only the final
        # result
        return_code = super().check()

        # we verify that source directory and target repository are correct
        return_code |= self.source.check()
        return_code |= self.target.check()

        return return_code

    def setup(self):
        # in setup we return as soon as we detect an issue to avoid changing
        # too much
        return_code = super().setup()
        if return_code != 0:
            return return_code

        return_code = self.source.setup()
        if return_code != 0:
            return return_code

        return_code = self.target.setup()
        if return_code != 0:
            return return_code

        # TODO validate how much of the following lines and methods
        # should go into the directory/repository modules
        SetConnections.BackupInitConnections(self.source.base_dir.conn,
                                             self.target.base_dir.conn)
        self.target.base_dir.conn.fs_abilities.backup_set_globals(
            self.source.base_dir, self.values.force)
        self.target.init_quoting(self.values.chars_to_quote)
        self._init_user_group_mapping(self.target.base_dir.conn)
        previous_time = self.target.get_mirror_time()
        if previous_time >= Time.curtime:
            log.Log("The last backup is not in the past. Aborting.",
                    log.ERROR)
            return 1
        if log.Log.verbosity > 0:
            try:  # the target repository must be writable
                log.Log.open_logfile(
                    self.target.data_dir.append("backup.log"))
            except (log.LoggerError, Security.Violation) as exc:
                log.Log("Unable to open logfile due to '{ex}'".format(
                    ex=exc), log.ERROR)
                return 1
        log.ErrorLog.open(Time.curtimestr, compress=self.values.compression)

        (select_opts, select_data) = selection.get_prepared_selections(
            self.values.selections)
        self.source.set_select(select_opts, select_data)
        self._warn_if_infinite_recursion(self.source.base_dir,
                                         self.target.base_dir)

        return 0

    def run(self):
        # do regress the target directory if necessary
        if self.target.needs_regress():
            ret_code = self.target.regress()
            if ret_code != 0:
                return ret_code
        previous_time = self.target.get_mirror_time()
        if previous_time < 0 or previous_time >= Time.curtime:
            log.Log("Either there is more than one current_mirror or "
                    "the last backup is not in the past. Aborting.",
                    log.ERROR)
            return 1
        if Globals.get_api_version() < 201:  # compat200
            if previous_time:
                Time.setprevtime(previous_time)
                self.target.base_dir.conn.Main.backup_touch_curmirror_local(
                    self.source.base_dir, self.target.base_dir)
                backup.mirror_and_increment_compat200(
                    self.source.base_dir, self.target.base_dir,
                    self.target.incs_dir)
                self.target.base_dir.conn.Main.backup_remove_curmirror_local()
            else:
                backup.mirror_compat200(
                    self.source.base_dir, self.target.base_dir)
                self.target.base_dir.conn.Main.backup_touch_curmirror_local(
                    self.source.base_dir, self.target.base_dir)
            self.target.base_dir.conn.Main.backup_close_statistics(time.time())
        else:  # API 201 and higher
            if previous_time:
                Time.setprevtime(previous_time)
            self._operate_backup(previous_time)

        return 0

    def _operate_backup(self, previous_time=None):
        """
        Execute the actual backup operation
        """
        log.Log(
            "Starting backup operation from source path {sp} to destination "
            "path {dp}".format(sp=self.source.base_dir,
                               dp=self.target.base_dir), log.NOTE)

        if previous_time:
            self.target.touch_current_mirror(Time.curtimestr)

        source_rpiter = self.source.get_select()
        self.target.set_rorp_cache(source_rpiter, previous_time)
        dest_sigiter = self.target.get_sigs()
        source_diffiter = self.source.get_diffs(dest_sigiter)
        self.target.patch_or_increment(source_diffiter, previous_time)

        if previous_time:
            self.target.remove_current_mirror()
        else:
            self.target.touch_current_mirror(Time.curtimestr)

        self.target.close_statistics(time.time())

    def _warn_if_infinite_recursion(self, rpin, rpout):
        """
        Warn user if target location is contained in source location
        """
        # Just a few heuristics, we don't have to get every case
        if rpout.conn is not rpin.conn:
            return
        if len(rpout.path) <= len(rpin.path) + 1:
            return
        if rpout.path[:len(rpin.path) + 1] != rpin.path + b'/':
            return

        # relative_rpout_comps = tuple(
        #     rpout.path[len(rpin.path) + 1:].split(b'/'))
        # relative_rpout = rpin.new_index(relative_rpout_comps)
        # FIXME: this fails currently because the selection object isn't stored
        #        but an iterable, the object not being pickable.
        #        Related to issue #296
        # if not Globals.select_mirror.Select(relative_rpout):
        #     return

        log.Log("The target directory '{td}' may be contained in the "
                "source directory '{sd}'. "
                "This could cause an infinite recursion. "
                "You may need to use the --exclude option "
                "(which you might already have done).".format(
                    td=rpout, sd=rpin), log.WARNING)


def get_action_class():
    return BackupAction
