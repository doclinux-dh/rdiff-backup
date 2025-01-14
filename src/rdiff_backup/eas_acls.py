# Copyright 2003 Ben Escoto
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
"""Store and retrieve extended attributes and access control lists

Not all file systems will have EAs and ACLs, but if they do, store
this information in separate files in the rdiff-backup-data directory,
called extended_attributes.<time>.snapshot and
access_control_lists.<time>.snapshot.

"""

import base64
import errno
import re
import io
import os
try:
    import posix1e
except ImportError:
    pass
from . import Globals, connection, metadata, rorpiter, log, C, rpath, user_group  # noqa: F401

# When an ACL gets dropped, put name in dropped_acl_names.  This is
# only used so that only the first dropped ACL for any given name
# triggers a warning.
dropped_acl_names = {}


class ExtendedAttributes:
    """Hold a file's extended attribute information"""

    def __init__(self, index, attr_dict=None):
        """Initialize EA object with no attributes"""
        self.index = index
        if attr_dict is None:
            self.attr_dict = {}
        else:
            self.attr_dict = attr_dict

    def __eq__(self, ea):
        """Equal if all attributes are equal"""
        assert isinstance(ea, ExtendedAttributes), (
            "You can only compare with extended attributes not with {otype}.".format(
                otype=type(ea)))
        return ea.attr_dict == self.attr_dict

    def __ne__(self, ea):
        return not self.__eq__(ea)

    def get_indexpath(self):
        return self.index and b'/'.join(self.index) or b'.'

    def read_from_rp(self, rp):
        """Set the extended attributes from an rpath"""
        try:
            attr_list = rp.conn.xattr.list(rp.path, rp.issym())
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.EPERM, errno.ETXTBSY):
                return  # if not supported, consider empty
            if exc.errno in (errno.EACCES, errno.ENOENT, errno.ELOOP):
                log.Log("Listing extended attributes of path {pa} produced "
                        "exception '{ex}', ignored".format(pa=rp, ex=exc),
                        log.INFO)
                return
            raise
        for attr in attr_list:
            if attr.startswith(b'system.'):
                # Do not preserve system extended attributes
                continue
            if not rp.isdir() and attr == b'com.apple.ResourceFork':
                # Resource Fork handled elsewhere, except for directories
                continue
            try:
                self.attr_dict[attr] = \
                    rp.conn.xattr.get(rp.path, attr, rp.issym())
            except OSError as exc:
                # File probably modified while reading, just continue
                if exc.errno == errno.ENODATA:
                    continue
                elif exc.errno == errno.ENOENT:
                    break
                    # Handle bug in pyxattr < 0.2.2
                elif exc.errno == errno.ERANGE:
                    continue
                else:
                    raise

    def write_to_rp(self, rp):
        """Write extended attributes to rpath rp"""
        self._clear_rp(rp)
        for (name, value) in self.attr_dict.items():
            try:
                rp.conn.xattr.set(rp.path, name, value, 0, rp.issym())
            except OSError as exc:
                # Mac and Linux attributes have different namespaces, so
                # fail gracefully if can't call xattr.set
                if exc.errno in (errno.EOPNOTSUPP, errno.EPERM, errno.EACCES,
                                 errno.ENOENT, errno.EINVAL):
                    log.Log("Unable to write xattr {xa} to path {pa} "
                            "due to exception '{ex}', ignoring".format(
                                xa=name, pa=rp, ex=exc), log.INFO)
                    continue
                else:
                    raise

    def get(self, name):
        """Return attribute attached to given name"""
        return self.attr_dict[name]

    def set(self, name, value=b""):
        """Set given name to given value.  Does not write to disk"""
        self.attr_dict[name] = value

    def delete(self, name):
        """Delete value associated with given name"""
        del self.attr_dict[name]

    def is_empty(self):
        """Return true if no extended attributes are set"""
        return not self.attr_dict

    def _clear_rp(self, rp):
        """Delete all the extended attributes in rpath"""
        try:
            for name in rp.conn.xattr.list(rp.path, rp.issym()):
                try:
                    rp.conn.xattr.remove(rp.path, name, rp.issym())
                except PermissionError:  # errno.EACCES
                    # SELinux attributes cannot be removed, and we don't want
                    # to bail out or be too noisy at low log levels.
                    log.Log("Not allowed to remove extended attribute "
                            "{ea} from path {pa}".format(ea=name, pa=rp),
                            log.DEBUG)
                    continue
                except OSError as exc:
                    # can happen because trusted.SGI_ACL_FILE is deleted
                    # together with system.posix_acl_access on XFS file systems.
                    if exc.errno == errno.ENODATA:
                        continue
                    else:  # can be anything, just fail
                        raise
        except io.UnsupportedOperation:  # errno.EOPNOTSUPP or errno.EPERM
            return  # if not supported, consider empty
        except FileNotFoundError as exc:
            log.Log("Unable to clear extended attributes on path {pa} due to "
                    "exception '{ex}', ignoring".format(pa=rp, ex=exc),
                    log.NOTE)
            return


class EAExtractor(metadata.FlatExtractor):
    """Iterate ExtendedAttributes objects from the EA information file"""
    record_boundary_regexp = re.compile(b'(?:\\n|^)(# file: (.*?))\\n')

    @staticmethod
    def _record_to_object(record):
        """Convert text record to ExtendedAttributes object"""
        lines = record.split(b'\n')
        first = lines.pop(0)
        if not first[:8] == b'# file: ':
            raise metadata.ParsingError("Bad record beginning: %r" % first[:8])
        filename = first[8:]
        if filename == b'.':
            index = ()
        else:
            unquoted_filename = C.acl_unquote(filename)
            index = tuple(unquoted_filename.split(b'/'))
        ea = ExtendedAttributes(index)

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line[0] == b'#':
                raise metadata.ParsingError(
                    "Only the first line of a record can start with a hash: {line}.".format(
                        line=line))
            eq_pos = line.find(b'=')
            if eq_pos == -1:
                ea.set(line)
            else:
                name = line[:eq_pos]
                if line[eq_pos + 1:eq_pos + 3] != b'0s':
                    raise metadata.ParsingError(
                        "Currently only base64 encoding supported")
                encoded_val = line[eq_pos + 3:]
                ea.set(name, base64.b64decode(encoded_val))
        return ea

    def _filename_to_index(self, filename):
        """Convert possibly quoted filename to index tuple"""
        if filename == b'.':
            return ()
        else:
            return tuple(C.acl_unquote(filename).split(b'/'))


class ExtendedAttributesFile(metadata.FlatFile):
    """Store/retrieve EAs from extended_attributes file"""
    _prefix = b"extended_attributes"
    _extractor = EAExtractor

    @staticmethod
    def _object_to_record(ea):
        """Convert ExtendedAttributes object to text record"""
        str_list = [b'# file: %s' % C.acl_quote(ea.get_indexpath())]

        for (name, val) in ea.attr_dict.items():
            if not val:
                str_list.append(name)
            else:
                encoded_val = base64.b64encode(val)
                str_list.append(b'%s=0s%s' % (C.acl_quote(name), encoded_val))
        return b'\n'.join(str_list) + b'\n'


def join_ea_iter(rorp_iter, ea_iter):
    """Update a rorp iter by adding the information from ea_iter"""

    def _safe_str(cmd):
        """Transform bytes into string without risk of conversion error"""
        if isinstance(cmd, str):
            return cmd
        else:
            return str(cmd, errors='replace')

    for rorp, ea in rorpiter.CollateIterators(rorp_iter, ea_iter):
        assert rorp, ("Missing rorp for EA index '{eaidx}'.".format(
            eaidx=_safe_str(ea.index)))
        if not ea:
            ea = ExtendedAttributes(rorp.index)
        rorp.set_ea(ea)
        yield rorp


class AccessControlLists:
    """Hold a file's access control list information

    Since posix1e.ACL objects cannot be pickled, and because they lack
    user/group name information, store everything in self.entry_list
    and self.default_entry_list.

    """
    # permissions regular expression
    _perm_re = re.compile(r"[r-][w-][x-]")

    def __init__(self, index, acl_text=None):
        """Initialize object with index and possibly acl_text"""
        self.index = index
        if acl_text:
            self._set_from_text(acl_text)
        else:
            self.entry_list = self.default_entry_list = None

    def __str__(self):
        """Return text version of acls"""
        if not self.entry_list:
            return ""
        slist = list(map(self._entrytuple_to_text, self.entry_list))
        if self.default_entry_list:
            slist.extend([
                "default:" + self._entrytuple_to_text(e)
                for e in self.default_entry_list
            ])
        return "\n".join(slist)

    def __eq__(self, acl):
        """Compare self and other access control list

        Basic acl permissions are considered equal to an empty acl
        object.

        """
        assert isinstance(acl, self.__class__), (
            "ACLs can only be compared with ACLs not {otype}.".format(
                otype=type(acl)))
        if self.is_basic():
            return acl.is_basic()
        return (self._cmp_entry_list(self.entry_list, acl.entry_list)
                and self._cmp_entry_list(self.default_entry_list,
                                         acl.default_entry_list))

    def __ne__(self, acl):
        return not self.__eq__(acl)

    def get_indexpath(self):
        return self.index and b'/'.join(self.index) or b'.'

    def is_basic(self):
        """True if acl can be reduced to standard unix permissions

        Assume that if they are only three entries, they correspond to
        user, group, and other, and thus don't use any special ACL
        features.

        """
        if not self.entry_list and not self.default_entry_list:
            return True
        assert len(self.entry_list) >= 3, (
            "Too few ACL entries '{ent}', must be 3 or more.".format(
                ent=self.entry_list))
        return len(self.entry_list) == 3 and not self.default_entry_list

    def read_from_rp(self, rp):
        """Set self.ACL from an rpath, or None if not supported"""
        self.entry_list, self.default_entry_list = \
            rp.conn.eas_acls.get_acl_lists_from_rp(rp)

    def write_to_rp(self, rp, map_names=1):
        """Write current access control list to RPath rp"""
        rp.conn.eas_acls.set_rp_acl(rp, self.entry_list,
                                    self.default_entry_list, map_names)

    def _set_from_text(self, text):
        """Set self.entry_list and self.default_entry_list from text"""
        self.entry_list, self.default_entry_list = [], []
        for line in text.split('\n'):
            comment_pos = line.find('#')
            if comment_pos >= 0:
                line = line[:comment_pos]
            line = line.strip()
            if not line:
                continue

            if line.startswith('default:'):
                entrytuple = self._text_to_entrytuple(line[8:])
                self.default_entry_list.append(entrytuple)
            else:
                self.entry_list.append(self._text_to_entrytuple(line))

    def _entrytuple_to_text(self, entrytuple):
        """Return text version of entrytuple, as in getfacl, or
        raise ValueError exception if input is wrong."""
        tagchar, name_pair, perms = entrytuple
        if tagchar == "U":
            text = 'user::'
        elif tagchar == "u":
            uid, uname = name_pair
            text = 'user:%s:' % (uname or uid)
        elif tagchar == "G":
            text = 'group::'
        elif tagchar == "g":
            gid, gname = name_pair
            text = 'group:%s:' % (gname or gid)
        elif tagchar == "M":
            text = 'mask::'
        elif tagchar == "O":
            text = 'other::'
        else:
            raise ValueError(
                "The tag {tag} must be a character from [UuGgMO].".format(
                    tag=tagchar))

        permstring = '%s%s%s' % (perms & 4 and 'r' or '-', perms & 2 and 'w'
                                 or '-', perms & 1 and 'x' or '-')
        return text + permstring

    def _text_to_entrytuple(self, text):
        """Return entrytuple given text like 'user:foo:r--'

        See the _acl_to_list function for entrytuple documentation.

        """
        typetext, qualifier, permtext = text.split(':')
        if qualifier:
            try:
                uid = int(qualifier)
            except ValueError:
                namepair = (None, qualifier)
            else:
                namepair = (uid, None)

            if typetext == 'user':
                typechar = "u"
            elif typetext == 'group':
                typechar = "g"
            else:
                raise ValueError("Type {atype} of ACL must be one of user or "
                                 "group if qualifier {qual} is present.".format(
                                     atype=typetext, qual=qualifier))
        else:
            namepair = None
            if typetext == 'user':
                typechar = "U"
            elif typetext == 'group':
                typechar = "G"
            elif typetext == 'mask':
                typechar = "M"
            elif typetext == 'other':
                typechar = "O"
            else:
                raise ValueError("Type {atype} of ACL must be one of user, group, "
                                 "mask or other if qualifier is absent.".format(
                                     atype=typetext))

        if not self._perm_re.fullmatch(permtext):
            raise ValueError(
                "Permission {perm} in ACLs must be a three "
                "characters string made of 'rwx' or dashes.".format(
                    perm=permtext))
        read, write, execute = permtext
        perms = ((read == 'r') << 2 | (write == 'w') << 1 | (execute == 'x'))
        return (typechar, namepair, perms)

    def _cmp_entry_list(self, l1, l2):
        """True if the lists have same entries.  Assume preordered"""
        if not l1:
            return not l2
        if not l2 or len(l1) != len(l2):
            return 0
        for i in range(len(l1)):
            type1, namepair1, perms1 = l1[i]
            type2, namepair2, perms2 = l2[i]
            if type1 != type2 or perms1 != perms2:
                return 0
            if namepair1 == namepair2:
                continue
            if not namepair1 or not namepair2:
                return 0
            (id1, name1), (id2, name2) = namepair1, namepair2
            if name1:
                if name1 == name2:
                    continue
                else:
                    return 0
            if name2:
                return 0
            if id1 != id2:
                return 0
        return 1

    def _eq_verbose(self, acl):
        """Returns same as __eq__ but print explanation if not equal.
        TEST: This function is used solely as part of the test suite."""
        if not self.cmp_entry_list(self.entry_list, acl.entry_list):
            print("ACL entries for {rp} compare differently".format(rp=self))
            return 0
        if not self.cmp_entry_list(self.default_entry_list,
                                   acl.default_entry_list):
            print("Default ACL entries for {rp} do not compare".format(rp=self))
            return 0
        return 1


class ACLExtractor(EAExtractor):
    """Iterate AccessControlLists objects from the ACL information file

    Except for the _record_to_object method, we can reuse everything in
    the EAExtractor class because the file formats are so similar.

    """

    @staticmethod
    def _record_to_object(record):
        """Convert text record to an AccessControlLists object"""
        newline_pos = record.find(b'\n')
        first_line = record[:newline_pos]
        if not first_line.startswith(b'# file: '):
            raise metadata.ParsingError("Bad record beginning: %r" % first_line)
        filename = first_line[8:]
        if filename == b'.':
            index = ()
        else:
            unquoted_filename = C.acl_unquote(filename)
            index = tuple(unquoted_filename.split(b'/'))
        return AccessControlLists(index, os.fsdecode(record[newline_pos:]))


class AccessControlListFile(metadata.FlatFile):
    """Store/retrieve ACLs from extended attributes file"""
    _prefix = b'access_control_lists'
    _extractor = ACLExtractor

    @staticmethod
    def _object_to_record(acl):
        """Convert an AccessControlLists object into a text record"""
        return b'# file: %b\n%b\n' % (C.acl_quote(acl.get_indexpath()),
                                      os.fsencode(str(acl)))


# @API(set_rp_acl, 200)
def set_rp_acl(rp, entry_list=None, default_entry_list=None, map_names=1):
    """Set given rp with ACL that acl_text defines.  rp should be local"""
    assert rp.conn is Globals.local_connection, (
        "Set ACLs of path should only be done locally not over {conn}.".format(
            conn=rp.conn))
    if entry_list:
        acl = _list_to_acl(entry_list, map_names)
    else:
        acl = posix1e.ACL()

    try:
        acl.applyto(rp.path)
    except OSError as exc:
        if exc.errno == errno.EOPNOTSUPP:
            log.Log(
                "Unable to set ACL on path {pa} due to exception '{ex}'".format(
                    pa=rp, ex=exc), log.INFO)
            return
        else:
            raise

    if rp.isdir():
        if default_entry_list:
            def_acl = _list_to_acl(default_entry_list, map_names)
        else:
            def_acl = posix1e.ACL()
        def_acl.applyto(rp.path, posix1e.ACL_TYPE_DEFAULT)


# @API(get_acl_lists_from_rp, 200)
def get_acl_lists_from_rp(rp):
    """Returns (acl_list, def_acl_list) from an rpath.  Call locally"""
    assert rp.conn is Globals.local_connection, (
        "Get ACLs of path should only be done locally not over {conn}.".format(
            conn=rp.conn))
    try:
        acl = posix1e.ACL(file=rp.path)
    except (FileNotFoundError, UnicodeEncodeError) as exc:
        log.Log(
            "Unable to read ACL from path {pa} due to exception '{ex}'".format(
                pa=rp, ex=exc), log.NOTE)
        acl = None
    except OSError as exc:
        if exc.errno == errno.EOPNOTSUPP:
            acl = None
        else:
            raise
    if rp.isdir():
        try:
            def_acl = posix1e.ACL(filedef=os.fsdecode(rp.path))
        except (FileNotFoundError, UnicodeEncodeError) as exc:
            log.Log("Unable to read default ACL from path {pa} due to "
                    "exception '{ex}'".format(pa=rp, ex=exc), log.NOTE)
            def_acl = None
        except OSError as exc:
            if exc.errno == errno.EOPNOTSUPP:
                def_acl = None
            else:
                raise
    else:
        def_acl = None
    return (acl and _acl_to_list(acl), def_acl and _acl_to_list(def_acl))


def join_acl_iter(rorp_iter, acl_iter):
    """Update a rorp iter by adding the information from acl_iter"""
    for rorp, acl in rorpiter.CollateIterators(rorp_iter, acl_iter):
        assert rorp, ("Missing rorp for ACL index '{aidx}'.".format(
            aidx=acl.index))
        if not acl:
            acl = AccessControlLists(rorp.index)
        rorp.set_acl(acl)
        yield rorp


def _acl_to_list(acl):
    """Return list representation of posix1e.ACL object

    ACL objects cannot be pickled, so this representation keeps
    the structure while adding that option.  Also we insert the
    username along with the id, because that information will be
    lost when moved to another system.

    The result will be a list of tuples.  Each tuple will have the
    form (acltype, (uid or gid, uname or gname) or None, permissions
    as an int).  acltype is encoded as a single character:

    U - ACL_USER_OBJ
    u - ACL_USER
    G - ACL_GROUP_OBJ
    g - ACL_GROUP
    M - ACL_MASK
    O - ACL_OTHER

    """

    def acltag_to_char(tag):
        if tag == posix1e.ACL_USER_OBJ:
            return "U"
        elif tag == posix1e.ACL_USER:
            return "u"
        elif tag == posix1e.ACL_GROUP_OBJ:
            return "G"
        elif tag == posix1e.ACL_GROUP:
            return "g"
        elif tag == posix1e.ACL_MASK:
            return "M"
        elif tag == posix1e.ACL_OTHER:
            return "O"
        else:
            raise ValueError("Unknown ACL tag {atag}.".format(atag=tag))

    def entry_to_tuple(entry):
        tagchar = acltag_to_char(entry.tag_type)
        if tagchar == "u":
            uid = entry.qualifier
            owner_pair = (uid, user_group.uid2uname(uid))
        elif tagchar == "g":
            gid = entry.qualifier
            owner_pair = (gid, user_group.gid2gname(gid))
        else:
            owner_pair = None

        perms = (entry.permset.read << 2 | entry.permset.write << 1
                 | entry.permset.execute)
        return (tagchar, owner_pair, perms)

    return list(map(entry_to_tuple, acl))


def _list_to_acl(entry_list, map_names=1):
    """Return posix1e.ACL object from list representation

    If map_names is true, use user_group to update the names for the
    current system, and drop if not available.  Otherwise just use the
    same id.

    See the _acl_to_list function for the format of an acllist.

    """

    def char_to_acltag(typechar):
        """Given typechar, query posix1e module for appropriate constant"""
        if typechar == "U":
            return posix1e.ACL_USER_OBJ
        elif typechar == "u":
            return posix1e.ACL_USER
        elif typechar == "G":
            return posix1e.ACL_GROUP_OBJ
        elif typechar == "g":
            return posix1e.ACL_GROUP
        elif typechar == "M":
            return posix1e.ACL_MASK
        elif typechar == "O":
            return posix1e.ACL_OTHER
        else:
            raise ValueError(
                "Unknown ACL character {achar} (must be one of [UuGgMO]).".format(
                    achar=typechar))

    def warn_drop(name):
        """Warn about acl with name getting dropped"""
        global dropped_acl_names
        if Globals.never_drop_acls:
            log.Log.FatalError("--never-drop-acls specified but cannot map "
                               "ACL name {an}".format(an=name))
        if name in dropped_acl_names:
            return
        log.Log("ACL name {an} not found on system, dropping entry. "
                "Further ACL entries dropped with this name will not "
                "trigger further warnings".format(an=name), log.WARNING)
        dropped_acl_names[name] = name

    acl = posix1e.ACL()
    for typechar, owner_pair, perms in entry_list:
        id = None
        if owner_pair:
            if map_names:
                if typechar == "u":
                    id = user_group.acl_user_map(*owner_pair)
                elif typechar == "g":
                    id = user_group.acl_group_map(*owner_pair)
                else:
                    raise ValueError(
                        "Type '{tc}' must be one of 'u' or 'g'.".format(
                            tc=typechar))
                if id is None:
                    warn_drop(owner_pair[1])
                    continue
            else:
                assert owner_pair[0] is not None, (
                    "First owner can't be None with type={tc}, "
                    "owner pair={own}, perms={perms}".format(
                        tc=typechar, own=owner_pair, perms=perms))
                id = owner_pair[0]

        entry = posix1e.Entry(acl)
        entry.tag_type = char_to_acltag(typechar)
        if id is not None:
            entry.qualifier = id
        entry.permset.read = perms >> 2
        entry.permset.write = perms >> 1 & 1
        entry.permset.execute = perms & 1
    return acl


# FIXME overriding functions in the rpath module doesn't sound right


def rpath_acl_get(rp):
    """Get acls of given rpath rp.

    This overrides a function in the rpath module.

    """
    acl = AccessControlLists(rp.index)
    if not rp.issym():
        acl.read_from_rp(rp)
    return acl


rpath.acl_get = rpath_acl_get


def rpath_get_blank_acl(index):
    """Get a blank AccessControlLists object (override rpath function)"""
    return AccessControlLists(index)


rpath.get_blank_acl = rpath_get_blank_acl


def rpath_ea_get(rp):
    """Get extended attributes of given rpath

    This overrides a function in the rpath module.

    """
    ea = ExtendedAttributes(rp.index)
    if not rp.issock() and not rp.isfifo():
        ea.read_from_rp(rp)
    return ea


rpath.ea_get = rpath_ea_get


def rpath_get_blank_ea(index):
    """Get a blank ExtendedAttributes object (override rpath function)"""
    return ExtendedAttributes(index)


rpath.get_blank_ea = rpath_get_blank_ea
