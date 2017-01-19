from influxdb import InfluxDBClient
from collections import OrderedDict
import requests
from distutils.version import LooseVersion

import re



# TODO makes sense to abstract this to allow cacheable wrapper implementations
class QueryEngine(object):

    INFLUX_0_11 = LooseVersion('0.11.0')

    def __init__(self, config):
        influx_config = config.get('influxdb', {})

        host = influx_config.get('host', 'localhost')
        port = influx_config.get('port', '8086')
        ssl = influx_config.get('ssl', False)

        self.client = InfluxDBClient(host,
                                     port,
                                     influx_config.get('user', 'root'),
                                     influx_config.get('password', 'root'),
                                     influx_config.get('db', 'graphite'),
                                     ssl)

        self.config = config
        self.aggregate_dict = None
        self.steps = config.get('steps', [])

        self.minimum_step = self.steps[0][1] if self.steps else 10  # reasonable default?

        # This is a workaround for the Influx driver not supporting the `/ping` endpoint
        # correctly, we need the URL so we can hit it directly.
        self.influx_url = 'https://' if ssl else 'http://'
        self.influx_url = self.influx_url + host + ':' + str(port)

        self.influx_version = None

    def query(self, metrics, start_time, end_time):
        step = self.determine_interval(start_time, end_time)

        query_sets = self.build_query_sets(metrics)

        result = {
            'series': {},
            'from': start_time,
            'to': end_time,
            'step': step
        }

        for agg, metrics in query_sets.items():
            series_list = ', '.join(['"%s"' % path for path in metrics])
            influx_query = self.build_influx_query(series_list, start_time, end_time, agg, step)
            data = self.client.query(influx_query, {'epoch': 's'})
            for key in data.keys():
                result['series'][key[0]] = [d['value'] for d in data.get_points(key[0])]

        return result

    def get_series(self):
        data = self.client.query('SHOW MEASUREMENTS')

        if 'series' not in data.raw:
            return []

        # Influx changed the result format of `SHOW SERIES` in the 0.11 release
        if self.get_influx_version() >= self.INFLUX_0_11:
            return (v[0] for v in data.raw['series'][0]['values'])
        else:
            return (r['name'] for r in data.raw['series'])

    # Private

    def get_influx_version(self):
        # It appears that the influx python driver does not like the 204 response of the `/ping`
        # endpoint so we're sending the request directly.
        if not self.influx_version:
            version_string = requests.get(self.influx_url + '/ping').headers['X-Influxdb-Version']
            self.influx_version = LooseVersion(version_string)

        return self.influx_version


    def build_aggregate_dict(self):
        self.aggregate_dict = OrderedDict(
            (re.compile(reg), agg) for (reg, agg) in self.config.get('aggregates', [])
        )

    def lookup_aggregate(self, metric):
        if not self.aggregate_dict:
            self.build_aggregate_dict()

        for regex, agg in self.aggregate_dict.items():
            if regex.search(metric): return agg

        return 'mean'  # TODO: make default configurable

    def build_query_sets(self, metrics):
        query_sets = {}
        for metric in metrics:
            agg = self.lookup_aggregate(metric)
            if agg not in query_sets: query_sets[agg] = []
            query_sets[agg].append(metric)

        return query_sets

    def determine_interval(self, start_time, end_time):
        span = end_time - start_time

        final_step = self.minimum_step

        for limit, step in self.steps:
            if span >= limit:
                final_step = step
            else:
                break

        return final_step

    def build_influx_query(self, metrics, start_time, end_time, agg='mean', interval=300):
        return 'SELECT %s(value) AS value FROM %s WHERE time > %ds AND time <= %ds GROUP BY time(%ss)' % (agg, metrics, start_time, end_time, interval)
