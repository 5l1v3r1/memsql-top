#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2016 by MemSQL. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import urwid

from attrdict import AttrDict
from collections import namedtuple

from columns import CheckHasDataForAllColumns

def GetPlanCache(conn):
    #
    # We store the parameterized query and deparameterize it in GetPlanCache so
    # that we can filter out this query.
    #
    # We filter out queries where the plan_hash is null, because those
    # correspond to leaf queries with no corresponding aggregator.
    #
    GET_PLANCACHE_QUERY = "select database_name, query_text, plan_hash, " + \
                          "IFNULL(commits, 0) as commits, " + \
                          "IFNULL(rowcount, 0) as rowcount, " + \
                          "IFNULL(execution_time, 0) as execution_time, " + \
                          "IFNULL(queued_time, 0) as queued_time, " + \
                          "IFNULL(cpu_time, 0) as cpu_time, " + \
                          "IFNULL(memory_use, 0) as memory_use " + \
                          "from distributed_plancache_summary " + \
                          "where plan_hash is not null"

    rows = conn.query(GET_PLANCACHE_QUERY)
    return {
        r.plan_hash: r
        for r in rows if r.query_text != GET_PLANCACHE_QUERY
    }


def NormalizeDiffPlanCacheEntry(interval, database_name, query_text,
                                commits, rowcount, cpu_time, execution_time,
                                memory_use, queued_time):
    return CheckHasDataForAllColumns(AttrDict({
        'Database': database_name,
        'Query': query_text,
        'Executions/sec': float(commits) / interval,
        'RowCount/sec': float(rowcount) / interval,
        'CpuUtil': float(cpu_time) / 1000.0 / interval,
        'ExecutionTime/query': float(execution_time) / float(commits),
        'Memory/query': float(memory_use) / float(commits),
        'QueuedTime/query': float(queued_time) / float(commits)
    }))


def DiffPlanCache(new_plancache, old_plancache, interval):
    diff_plancache = {}
    for key, n_ent in new_plancache.items():
        if key not in old_plancache:
            #
            # It is possible for a plan cache entry to exist (or be newly
            # created) with a zero commit-count: for example, a slow query that
            # has not yet completed or erroring queries.
            #
            if n_ent.commits > 0:
                diff_plancache[key] = NormalizeDiffPlanCacheEntry(
                    interval,
                    database_name=n_ent.database_name,
                    query_text=n_ent.query_text,
                    commits=n_ent.commits,
                    rowcount=n_ent.rowcount,
                    execution_time=n_ent.execution_time,
                    cpu_time=n_ent.cpu_time,
                    memory_use=n_ent.memory_use,
                    queued_time=n_ent.queued_time)
        elif n_ent.commits - old_plancache[key].commits > 0:
            o_ent = old_plancache[key]
            diff_plancache[key] = NormalizeDiffPlanCacheEntry(
                interval,
                database_name=n_ent.database_name,
                query_text=n_ent.query_text,
                commits=n_ent.commits - o_ent.commits,
                rowcount=n_ent.rowcount - o_ent.rowcount,
                execution_time=n_ent.execution_time - o_ent.execution_time,
                cpu_time=n_ent.cpu_time - o_ent.cpu_time,
                memory_use=n_ent.memory_use - o_ent.memory_use,
                queued_time=n_ent.queued_time - o_ent.queued_time
            )
    return diff_plancache


class DatabasePoller(urwid.Widget):
    signals = ['plancache_changed', 'cpu_util_changed', 'mem_usage_changed']

    def __init__(self, conn, update_interval):
        self.conn = conn
        self.update_interval = update_interval
        self.plancache = GetPlanCache(self.conn)
        super(DatabasePoller, self).__init__()


    def poll(self, loop, _):
        loop.set_alarm_in(self.update_interval, self.poll)
        new_plancache = GetPlanCache(self.conn)

        diff_plancache = DiffPlanCache(new_plancache, self.plancache,
                                       self.update_interval)
        self._emit('plancache_changed', diff_plancache)
        self.plancache = new_plancache

        sum_cpu_util = sum(pe.CpuUtil for pe in diff_plancache.values())
        self._emit('cpu_util_changed', sum_cpu_util)

        # TODO(awreece) This isn't accurately max memory across the whole cluster.
        tsm_row = self.conn.get("show status like 'Total_server_memory'")
        self._emit('mem_usage_changed', float(tsm_row.Value.split(" ")[0]))