# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from datetime import datetime, timedelta
from hashlib import md5
from os import environ
from threading import Lock

from . import operations
from .bundle import Bundle
from .concurrency import WorkerPool
from .deps import (
    DummyItem,
    find_item,
)
from .exceptions import (
    DontCache,
    GracefulApplyException,
    ItemDependencyLoop,
    NodeLockedException,
    NoSuchBundle,
    RemoteException,
    RepositoryError,
    SkipNode,
)
from .group import GROUP_ATTR_DEFAULTS
from .itemqueue import ItemQueue
from .items import Item
from .lock import NodeLock
from .metadata import hash_metadata
from .utils import cached_property, names
from .utils.dicts import hash_statedict
from .utils.text import (
    blue,
    bold,
    cyan,
    force_text,
    format_duration,
    green,
    mark_for_translation as _,
    red,
    validate_name,
    yellow,
)
from .utils.ui import io


class ApplyResult(object):
    """
    Holds information about an apply run for a node.
    """
    def __init__(self, node, item_results):
        self.node_name = node.name
        self.correct = 0
        self.fixed = 0
        self.skipped = 0
        self.failed = 0
        self.total = 0

        for item_id, result, duration in item_results:
            self.total += 1
            if result == Item.STATUS_ACTION_SUCCEEDED:
                self.correct += 1
            elif result == Item.STATUS_OK:
                self.correct += 1
            elif result == Item.STATUS_FIXED:
                self.fixed += 1
            elif result == Item.STATUS_SKIPPED:
                self.skipped += 1
            elif result == Item.STATUS_FAILED:
                self.failed += 1
            else:
                raise RuntimeError(_(
                    "can't make sense of results for {} on {}: {}"
                ).format(item_id, self.node_name, result))

        self.start = None
        self.end = None

    @property
    def duration(self):
        return self.end - self.start


def format_node_result(result):
    output = []
    output.append(("{count} OK").format(count=result.correct))

    if result.fixed:
        output.append(green(_("{count} fixed").format(count=result.fixed)))
    else:
        output.append(_("{count} fixed").format(count=result.fixed))

    if result.skipped:
        output.append(yellow(_("{count} skipped").format(count=result.skipped)))
    else:
        output.append(_("{count} skipped").format(count=result.skipped))

    if result.failed:
        output.append(red(_("{count} failed").format(count=result.failed)))
    else:
        output.append(_("{count} failed").format(count=result.failed))

    return ", ".join(output)


def handle_apply_result(node, item, status_code, interactive, details=None):
    if status_code == Item.STATUS_SKIPPED and details in (
        Item.SKIP_REASON_NO_TRIGGER,
        Item.SKIP_REASON_UNLESS,
    ):
        # skipped for "unless" or "not triggered", don't output those
        return
    formatted_result = format_item_result(
        status_code,
        node.name,
        item.bundle.name if item.bundle else "",  # dummy items don't have bundles
        item.id,
        interactive=interactive,
        details=details,
    )
    if formatted_result is not None:
        if status_code == Item.STATUS_FAILED:
            io.stderr(formatted_result)
        else:
            io.stdout(formatted_result)


def apply_items(
    node,
    autoskip_selector="",
    my_soft_locks=(),
    other_peoples_soft_locks=(),
    workers=1,
    interactive=False,
):
    item_queue = ItemQueue(node.items, node.name, node.os, node.os_version)
    # the item queue might contain new generated items (canned actions,
    # dummy items); adjust progress total accordingly
    extra_items = len(item_queue.all_items) - len(node.items)
    io.progress_increase_total(increment=extra_items)

    results = []

    def tasks_available():
        return bool(item_queue.items_without_deps)

    def next_task():
        item = item_queue.pop()
        return {
            'task_id': "{}:{}".format(node.name, item.id),
            'target': item.apply,
            'kwargs': {
                'autoskip_selector': autoskip_selector,
                'my_soft_locks': my_soft_locks,
                'other_peoples_soft_locks': other_peoples_soft_locks,
                'interactive': interactive,
            },
        }

    def handle_result(task_id, return_value, duration):
        item_id = task_id.split(":", 1)[1]
        item = find_item(item_id, item_queue.pending_items)

        status_code, details = return_value

        if status_code == Item.STATUS_FAILED:
            for skipped_item in item_queue.item_failed(item):
                handle_apply_result(
                    node,
                    skipped_item,
                    Item.STATUS_SKIPPED,
                    interactive,
                    details=Item.SKIP_REASON_DEP_FAILED,
                )
                results.append((skipped_item.id, Item.STATUS_SKIPPED, timedelta(0)))
        elif status_code in (Item.STATUS_FIXED, Item.STATUS_ACTION_SUCCEEDED):
            item_queue.item_fixed(item)
        elif status_code == Item.STATUS_OK:
            item_queue.item_ok(item)
        elif status_code == Item.STATUS_SKIPPED:
            for skipped_item in item_queue.item_skipped(item):
                skip_reason = Item.SKIP_REASON_DEP_SKIPPED
                for lock in other_peoples_soft_locks:
                    for selector in lock['items']:
                        if skipped_item.covered_by_autoskip_selector(selector):
                            skip_reason = Item.SKIP_REASON_SOFTLOCK
                            break
                handle_apply_result(
                    node,
                    skipped_item,
                    Item.STATUS_SKIPPED,
                    interactive,
                    details=skip_reason,
                )
                results.append((skipped_item.id, Item.STATUS_SKIPPED, timedelta(0)))
        else:
            raise AssertionError(_(
                "unknown item status returned for {item}: {status}".format(
                    item=item.id,
                    status=repr(status_code),
                ),
            ))

        handle_apply_result(node, item, status_code, interactive, details=details)
        io.progress_advance()
        if not isinstance(item, DummyItem):
            results.append((item.id, status_code, duration))

    worker_pool = WorkerPool(
        tasks_available,
        next_task,
        handle_result=handle_result,
        pool_id="apply_{}".format(node.name),
        workers=workers,
    )
    worker_pool.run()

    # we have no items without deps left and none are processing
    # there must be a loop
    if item_queue.items_with_deps:
        raise ItemDependencyLoop(item_queue.items_with_deps)

    return results


def _flatten_group_hierarchy(groups):
    """
    Takes a list of groups and returns a list of group names ordered so
    that parent groups will appear before any of their subgroups.
    """
    # dict mapping groups to subgroups
    child_groups = {}
    for group in groups:
        child_groups[group.name] = list(names(group.subgroups))

    # dict mapping groups to parent groups
    parent_groups = {}
    for child_group in child_groups.keys():
        parent_groups[child_group] = []
        for parent_group, subgroups in child_groups.items():
            if child_group in subgroups:
                parent_groups[child_group].append(parent_group)

    order = []

    while True:
        top_level_group = None
        for group, parents in parent_groups.items():
            if parents:
                continue
            else:
                top_level_group = group
                break
        if not top_level_group:
            if parent_groups:
                raise RuntimeError(
                    _("encountered subgroup loop that should have been detected")
                )
            else:
                break
        order.append(top_level_group)
        del parent_groups[top_level_group]
        for group in parent_groups.keys():
            if top_level_group in parent_groups[group]:
                parent_groups[group].remove(top_level_group)

    return order


def format_item_result(result, node, bundle, item, interactive=False, details=None):
    if details is True:
        details_text = "({})".format(_("create"))
    elif details is False:
        details_text = "({})".format(_("remove"))
    elif details is None:
        details_text = ""
    elif result == Item.STATUS_SKIPPED:
        details_text = "({})".format(Item.SKIP_REASON_DESC[details])
    else:
        details_text = "({})".format(", ".join(sorted(details)))
    if result == Item.STATUS_FAILED:
        return "{x} {node}  {bundle}  {item} {status} {details}".format(
            bundle=bold(bundle),
            details=details_text,
            item=item,
            node=bold(node),
            status=red(_("failed")),
            x=bold(red("✘")),
        )
    elif result == Item.STATUS_ACTION_SUCCEEDED:
        return "{x} {node}  {bundle}  {item} {status}".format(
            bundle=bold(bundle),
            item=item,
            node=bold(node),
            status=green(_("succeeded")),
            x=bold(green("✓")),
        )
    elif result == Item.STATUS_SKIPPED:
        return "{x} {node}  {bundle}  {item} {status} {details}".format(
            bundle=bold(bundle),
            details=details_text,
            item=item,
            node=bold(node),
            x=bold(yellow("»")),
            status=yellow(_("skipped")),
        )
    elif result == Item.STATUS_FIXED:
        return "{x} {node}  {bundle}  {item} {status} {details}".format(
            bundle=bold(bundle),
            details=details_text,
            item=item,
            node=bold(node),
            x=bold(green("✓")),
            status=green(_("fixed")),
        )


class Node(object):
    OS_FAMILY_BSD = (
        'freebsd',
        'macos',
        'netbsd',
        'openbsd',
    )
    OS_FAMILY_DEBIAN = (
        'debian',
        'ubuntu',
        'raspbian',
    )
    OS_FAMILY_REDHAT = (
        'rhel',
        'centos',
        'fedora',
        'oraclelinux',
    )

    OS_FAMILY_LINUX = (
        'amazonlinux',
        'arch',
        'opensuse',
        'openwrt',
        'gentoo',
        'linux',
    ) + \
        OS_FAMILY_DEBIAN + \
        OS_FAMILY_REDHAT

    OS_FAMILY_UNIX = OS_FAMILY_BSD + OS_FAMILY_LINUX
    OS_KNOWN = OS_FAMILY_UNIX + ('kubernetes',)

    def __init__(self, name, attributes=None):
        if attributes is None:
            attributes = {}

        if not validate_name(name):
            raise RepositoryError(_("'{}' is not a valid node name").format(name))

        self._add_host_keys = environ.get('BW_ADD_HOST_KEYS', False) == "1"
        self._bundles = attributes.get('bundles', [])
        self._compiling_metadata = Lock()
        self._dynamic_group_lock = Lock()
        self._dynamic_groups_resolved = False  # None means we're currently doing it
        self._metadata_so_far = {}
        self._node_metadata = attributes.get('metadata', {})
        self._ssh_conn_established = False
        self._ssh_first_conn_lock = Lock()
        self._template_node_name = attributes.get('template_node')
        self.hostname = attributes.get('hostname', name)
        self.name = name

        for attr in GROUP_ATTR_DEFAULTS:
            setattr(self, "_{}".format(attr), attributes.get(attr))

    def __lt__(self, other):
        return self.name < other.name

    def __repr__(self):
        return "<Node '{}'>".format(self.name)

    @cached_property
    def bundles(self):
        if self._dynamic_group_lock.acquire(False):
            self._dynamic_group_lock.release()
        else:
            raise RepositoryError(_(
                "node bundles cannot be queried with members_add/remove"
            ))
        with io.job(_("{node}  loading bundles").format(node=bold(self.name))):
            added_bundles = []
            found_bundles = []
            for group in self.groups:
                for bundle_name in group.bundle_names:
                    found_bundles.append(bundle_name)

            for bundle_name in found_bundles + list(self._bundles):
                if bundle_name not in added_bundles:
                    added_bundles.append(bundle_name)
                    try:
                        yield Bundle(self, bundle_name)
                    except NoSuchBundle:
                        raise NoSuchBundle(_(
                            "Node '{node}' wants bundle '{bundle}', but it doesn't exist."
                        ).format(
                            bundle=bundle_name,
                            node=self.name,
                        ))

    @cached_property
    def cdict(self):
        node_dict = {}
        for item in self.items:
            try:
                node_dict[item.id] = item.hash()
            except AttributeError:  # actions have no cdict
                pass
        return node_dict

    def covered_by_autoskip_selector(self, autoskip_selector):
        """
        True if this node should be skipped based on the given selector
        string (e.g. "node:foo,group:bar").
        """
        components = [c.strip() for c in autoskip_selector.split(",")]
        if "node:{}".format(self.name) in components:
            return True
        for group in self.groups:
            if "group:{}".format(group.name) in components:
                return True
        return False

    def group_membership_hash(self):
        return hash_statedict(sorted(names(self.groups)))

    @cached_property
    @io.job_wrapper(_("{}  determining groups").format(bold("{0.name}")))
    def groups(self):
        _groups = set(self.repo._static_groups_for_node(self))
        # lock to avoid infinite recursion when .members_add/remove
        # use stuff like node.in_group() that in turn calls this function
        if self._dynamic_group_lock.acquire(False):
            cache_result = True
            self._dynamic_groups_resolved = None
            # first we remove ourselves from all static groups whose
            # .members_remove matches us
            for group in list(_groups):
                if group.members_remove is not None and group.members_remove(self):
                    try:
                        _groups.remove(group)
                    except KeyError:
                        pass
            # now add all groups whose .members_add (but not .members_remove)
            # matches us
            _groups = _groups.union(self._groups_dynamic)
            self._dynamic_groups_resolved = True
            self._dynamic_group_lock.release()
        else:
            cache_result = False

        # we have to add parent groups at the very end, since we might
        # have added or removed subgroups thru .members_add/remove
        while True:
            # Since we're only looking at *immediate* parent groups,
            # we have to keep doing this until we stop adding parent
            # groups.
            _original_groups = _groups.copy()
            for group in list(_groups):
                for parent_group in group.immediate_parent_groups:
                    if cache_result:
                        with self._dynamic_group_lock:
                            self._dynamic_groups_resolved = None
                            if (
                                not parent_group.members_remove or
                                not parent_group.members_remove(self)
                            ):
                                _groups.add(parent_group)
                            self._dynamic_groups_resolved = True
                    else:
                        _groups.add(parent_group)
            if _groups == _original_groups:
                # we didn't add any new parent groups, so we can stop
                break

        if cache_result:
            return sorted(_groups)
        else:
            raise DontCache(sorted(_groups))

    @property
    def _groups_dynamic(self):
        """
        Returns all groups whose members_add matches this node.
        """
        _groups = set()
        for group in self.repo.groups:
            if group.members_add is not None and group.members_add(self):
                _groups.add(group)
            if group.members_remove is not None and group.members_remove(self):
                try:
                    _groups.remove(group)
                except KeyError:
                    pass
        return _groups

    def has_any_bundle(self, bundle_list):
        for bundle_name in bundle_list:
            if self.has_bundle(bundle_name):
                return True
        return False

    def has_bundle(self, bundle_name):
        for bundle in self.bundles:
            if bundle.name == bundle_name:
                return True
        return False

    def hash(self):
        return hash_statedict(self.cdict)

    def in_any_group(self, group_list):
        for group_name in group_list:
            if self.in_group(group_name):
                return True
        return False

    def in_group(self, group_name):
        for group in self.groups:
            if group.name == group_name:
                return True
        return False

    @cached_property
    def items(self):
        if not self.dummy:
            for bundle in self.bundles:
                for item in bundle.items:
                    yield item

    @cached_property
    def magic_number(self):
        return int(md5(self.name.encode('UTF-8')).hexdigest(), 16)

    def apply(
        self,
        autoskip_selector="",
        interactive=False,
        force=False,
        skip_list=tuple(),
        workers=4,
    ):
        if not list(self.items):
            io.stdout(_("{x} {node}  has no items").format(
                node=bold(self.name),
                x=yellow("»"),
            ))
            return None

        if self.covered_by_autoskip_selector(autoskip_selector):
            io.stdout(_("{x} {node}  skipped by --skip").format(
                node=bold(self.name),
                x=yellow("»"),
            ))
            return None

        if self.name in skip_list:
            io.stdout(_("{x} {node}  skipped by --resume-file").format(
                node=bold(self.name),
                x=yellow("»"),
            ))
            return None

        try:
            self.repo.hooks.node_apply_start(
                self.repo,
                self,
                interactive=interactive,
            )
        except SkipNode as exc:
            io.stdout(_("{x} {node}  skipped by hook ({reason})").format(
                node=bold(self.name),
                reason=str(exc) or _("no reason given"),
                x=yellow("»"),
            ))
            return None

        start = datetime.now()
        io.stdout(_("{x} {node}  {started} at {time}").format(
            node=bold(self.name),
            started=bold(_("started")),
            time=start.strftime("%Y-%m-%d %H:%M:%S"),
            x=blue("i"),
        ))

        error = False

        try:
            self.run("true")
        except RemoteException as exc:
            io.stdout(_("{x} {node}  Connection error: {msg}").format(
                msg=exc,
                node=bold(self.name),
                x=red("!"),
            ))
            error = _("Connection error (details above)")
            item_results = []
        else:
            try:
                with NodeLock(self, interactive=interactive, ignore=force) as lock:
                    item_results = apply_items(
                        self,
                        autoskip_selector=autoskip_selector,
                        my_soft_locks=lock.my_soft_locks,
                        other_peoples_soft_locks=lock.other_peoples_soft_locks,
                        workers=workers,
                        interactive=interactive,
                    )
            except NodeLockedException as e:
                if not interactive:
                    io.stderr(_(
                        "{x} {node} already locked by {user} at {date} ({duration} ago, "
                        "`bw apply -f` to override)"
                    ).format(
                        date=bold(e.args[0]['date']),
                        duration=e.args[0]['duration'],
                        node=bold(self.name),
                        user=bold(e.args[0]['user']),
                        x=red("!"),
                    ))
                error = _("Node locked (details above)")
                item_results = []
        result = ApplyResult(self, item_results)
        result.start = start
        result.end = datetime.now()

        io.stdout(_("{x} {node}  {completed} after {time}  ({stats})").format(
            completed=bold(_("completed")),
            node=bold(self.name),
            stats=format_node_result(result),
            time=format_duration(result.end - start),
            x=blue("i"),
        ))

        self.repo.hooks.node_apply_end(
            self.repo,
            self,
            duration=result.duration,
            interactive=interactive,
            result=result,
        )

        if error:
            raise GracefulApplyException(error)
        else:
            return result

    def download(self, remote_path, local_path):
        return operations.download(
            self.hostname,
            remote_path,
            local_path,
            add_host_keys=self._add_host_keys,
            wrapper_inner=self.cmd_wrapper_inner,
            wrapper_outer=self.cmd_wrapper_outer,
        )

    def get_item(self, item_id):
        return find_item(item_id, self.items)

    @property
    def metadata(self):
        """
        Returns full metadata for a node. MUST NOT be used from inside a
        metadata processor. Use .partial_metadata instead.
        """
        if self._dynamic_groups_resolved is None:
            # return only metadata set directly at the node level if
            # we're still in the process of figuring out which groups
            # we belong to
            return self._node_metadata
        else:
            return self.repo._metadata_for_node(self.name, partial=False)

    @property
    def metadata_blame(self):
        return self.repo._metadata_for_node(self.name, partial=False, blame=True)

    def metadata_hash(self):
        return hash_metadata(self.metadata)

    @property
    def metadata_processors(self):
        for bundle in self.bundles:
            for metadata_processor in bundle.metadata_processors:
                yield (
                    "{}.{}".format(
                        bundle.name,
                        metadata_processor.__name__,
                    ),
                    metadata_processor,
                )

    @property
    def partial_metadata(self):
        """
        Only to be used from inside metadata processors. Can't use the
        normal .metadata there because it might deadlock when nodes
        have interdependent metadata.

        It's OK for metadata processors to work with partial metadata
        because they will be fed all metadata updates until no more
        changes are made by any metadata processor.
        """
        return self.repo._metadata_for_node(self.name, partial=True)

    def run(self, command, data_stdin=None, may_fail=False, log_output=False):
        assert self.os in self.OS_FAMILY_UNIX

        if log_output:
            def log_function(msg):
                io.stdout("{x} {node}  {msg}".format(
                    node=bold(self.name),
                    msg=force_text(msg).rstrip("\n"),
                    x=cyan("›"),
                ))
        else:
            log_function = None

        if not self._ssh_conn_established:
            # Sometimes we're opening SSH connections to a node too fast
            # for OpenSSH to establish the ControlMaster socket for the
            # second and following connections to use.
            # To prevent this, we just wait until a first dummy command
            # has completed on the node before trying to reuse the
            # multiplexed connection.
            if self._ssh_first_conn_lock.acquire(False):
                try:
                    with io.job(_("{}  establishing connection...").format(bold(self.name))):
                        operations.run(self.hostname, "true", add_host_keys=self._add_host_keys)
                    self._ssh_conn_established = True
                finally:
                    self._ssh_first_conn_lock.release()
            else:
                # we didn't get the lock immediately, now we just wait
                # until it is released before we proceed
                with self._ssh_first_conn_lock:
                    pass

        return operations.run(
            self.hostname,
            command,
            add_host_keys=self._add_host_keys,
            data_stdin=data_stdin,
            ignore_failure=may_fail,
            log_function=log_function,
            wrapper_inner=self.cmd_wrapper_inner,
            wrapper_outer=self.cmd_wrapper_outer,
        )

    @property
    def template_node(self):
        if not self._template_node_name:
            return None
        else:
            target_node = self.repo.get_node(self._template_node_name)
            if target_node._template_node_name:
                raise RepositoryError(_(
                    "{template_node} cannot use template_node because {node} uses {template_node} "
                    "as template_node"
                ).format(node=self.name, template_node=target_node.name))
            else:
                return target_node

    def upload(self, local_path, remote_path, mode=None, owner="", group="", may_fail=False):
        assert self.os in self.OS_FAMILY_UNIX
        return operations.upload(
            self.hostname,
            local_path,
            remote_path,
            add_host_keys=self._add_host_keys,
            group=group,
            mode=mode,
            owner=owner,
            ignore_failure=may_fail,
            wrapper_inner=self.cmd_wrapper_inner,
            wrapper_outer=self.cmd_wrapper_outer,
        )

    def verify(self, show_all=False, workers=4):
        result = []
        start = datetime.now()

        if not self.items:
            io.stdout(_("{x} {node}  has no items").format(node=bold(self.name), x=yellow("!")))
        else:
            result = verify_items(self, show_all=show_all, workers=workers)

        return {
            'good': result.count(True),
            'bad': result.count(False),
            'unknown': result.count(None),
            'duration': datetime.now() - start,
        }


def build_attr_property(attr, default):
    def method(self):
        attr_source = None
        attr_value = None
        group_order = [
            self.repo.get_group(group_name)
            for group_name in _flatten_group_hierarchy(self.groups)
        ]

        for group in group_order:
            if getattr(group, attr) is not None:
                attr_source = "group:{}".format(group.name)
                attr_value = getattr(group, attr)

        if self.template_node:
            attr_source = "template_node"
            attr_value = getattr(self.template_node, attr)

        if getattr(self, "_{}".format(attr)) is not None:
            attr_source = "node"
            attr_value = getattr(self, "_{}".format(attr))

        if attr_value is None:
            attr_source = "default"
            attr_value = default

        io.debug(_("node {node} gets its {attr} attribute from: {source}").format(
            node=self.name,
            attr=attr,
            source=attr_source,
        ))
        if self._dynamic_groups_resolved:
            return attr_value
        else:
            raise DontCache(attr_value)
    method.__name__ = str("_group_attr_{}".format(attr))  # required for cached_property
                                                          # str() for Python 2 compatibility
    return cached_property(method)

for attr, default in GROUP_ATTR_DEFAULTS.items():
    setattr(Node, attr, build_attr_property(attr, default))


def verify_items(node, show_all=False, workers=1):
    items = []
    for item in node.items:
        if not item.triggered:
            items.append(item)
        elif not isinstance(item, DummyItem):
            io.progress_advance()

    try:
        node.run("true")
    except RemoteException as exc:
        io.stdout(_("{x} {node}  Connection error: {msg}").format(
            msg=exc,
            node=bold(node.name),
            x=red("!"),
        ))
        for item in items:
            io.progress_advance()
        return [None for item in items]

    def tasks_available():
        return bool(items)

    def next_task():
        while True:
            try:
                item = items.pop()
            except IndexError:
                return None
            if item._faults_missing_for_attributes:
                if item.error_on_missing_fault:
                    item._raise_for_faults()
                else:
                    io.progress_advance()
                    io.stdout(_("{x} {node}  {bundle}  {item}  ({msg})").format(
                        bundle=bold(item.bundle.name),
                        item=item.id,
                        msg=yellow(_("Fault unavailable")),
                        node=bold(node.name),
                        x=yellow("»"),
                    ))
            else:
                return {
                    'task_id': node.name + ":" + item.bundle.name + ":" + item.id,
                    'target': item.verify,
                }

    def handle_exception(task_id, exception, traceback):
        node_name, bundle_name, item_id = task_id.split(":", 2)
        io.progress_advance()
        if isinstance(exception, NotImplementedError):
            io.stdout(_("{x} {node}  {bundle}  {item}  (does not support verify)").format(
                bundle=bold(bundle_name),
                item=item_id,
                node=bold(node_name),
                x=cyan("?"),
            ))
        else:
            # Unlike with `bw apply`, it is OK for `bw verify` to encounter
            # exceptions when getting an item's status. `bw verify` doesn't
            # care about dependencies and therefore cannot know that looking
            # up a database user requires the database to be installed in
            # the first place.
            io.debug("exception while verifying {}:".format(task_id))
            io.debug(traceback)
            io.debug(repr(exception))
            io.stdout(_("{x} {node}  {bundle}  {item}  (unable to get status, check --debug for details)").format(
                bundle=bold(bundle_name),
                item=item_id,
                node=bold(node_name),
                x=cyan("?"),
            ))
        return None  # count this result as "unknown"

    def handle_result(task_id, return_value, duration):
        io.progress_advance()
        unless_result, item_status = return_value
        node_name, bundle_name, item_id = task_id.split(":", 2)
        if not unless_result and not item_status.correct:
            if item_status.must_be_created:
                details_text = _("create")
            elif item_status.must_be_deleted:
                details_text = _("remove")
            else:
                details_text = ", ".join(sorted(item_status.display_keys_to_fix))
            io.stderr("{x} {node}  {bundle}  {item} ({details})".format(
                bundle=bold(bundle_name),
                details=details_text,
                item=item_id,
                node=bold(node_name),
                x=red("✘"),
            ))
            return False
        else:
            if show_all:
                io.stdout("{x} {node}  {bundle}  {item}".format(
                    bundle=bold(bundle_name),
                    item=item_id,
                    node=bold(node_name),
                    x=green("✓"),
                ))
            return True

    worker_pool = WorkerPool(
        tasks_available,
        next_task,
        handle_result,
        handle_exception=handle_exception,
        pool_id="verify_{}".format(node.name),
        workers=workers,
    )
    return worker_pool.run()
