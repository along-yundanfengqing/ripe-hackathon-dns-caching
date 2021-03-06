from ripe.atlas.cousteau import AtlasResultsRequest, AtlasStream
import attr
import logging
from pprint import pprint as pp, pformat as pf
import pyasn
import base64
import dnslib
import click
import json
from ipaddress import ip_address
from enum import IntEnum
from datetime import datetime, timedelta

# TODO use ripestats for realtime info
asndb = pyasn.pyasn('ipasn.20170420.1200')
with open("../enrich-probe-info/prbid_to_info.json") as f:
    probe_db = json.load(f)

HOMEPROBE = 27635

"""
   * https://atlas.ripe.net/measurements/8310237/ (google 'whoami')
   * https://atlas.ripe.net/measurements/8310245/ (akamai 'whoami')
    * https://atlas.ripe.net/measurements/8310250/ (qname minimisation test)
    * https://atlas.ripe.net/measurements/8310360/ (TCP IPv4 capability)
    * https://atlas.ripe.net/measurements/8310364/ (TCP IPv6 capability)
    * https://atlas.ripe.net/measurements/8310366/ (IPv6 capability)
    * https://atlas.ripe.net/measurements/8311760/ (DNSSEC reference)
    * https://atlas.ripe.net/measurements/8311763/ (DNSSEC bogus)
    * https://atlas.ripe.net/measurements/8311777/ (NXDOMAIN hijacking)
    """

# TODO qname minin: would be nice to have ip as payload
# 'system-resolver-mangles-case', -> inform about 0x20 encoding?

# measurements to listen to
class MeasurementType(IntEnum):
    akamai_whois = 8310245
    google_whois = 8310237
    qname_minim = 8310250
    ipv4_tcp = 8310360
    ipv6_tcp = 8310364
    ipv6_cap = 8310366
    nxdomain_hijack = 8311777
    dnssec_reference = 8311760
    dnssec_bogus = 8311763

do_all = True
wanted_measurements = [MeasurementType.dnssec_bogus, MeasurementType.dnssec_reference], #MeasurementType.nxdomain_hijack]

def get_asn(ip):
    try:
        return asndb.lookup(ip)
    except ValueError as ex:
        _LOGGER.error("%s", ex)

def get_probe_info(probe_id):
    try:
        return probe_db[str(probe_id)]
    except:
        return None

logging.basicConfig(level=logging.INFO)
logging.getLogger("socketIO-client").setLevel(logging.WARNING) # silence socketio client
_LOGGER = logging.getLogger(__name__)
_LOGGER.info("wanted: %s" % wanted_measurements)

@attr.s
class ResolverInfo:
    ts = attr.ib()
    from_ip = attr.ib()
    from_probe = attr.ib()
    measurement_type = attr.ib()

    internal_resolvers = attr.ib()
    external_resolvers = attr.ib()
    resolver_asn = attr.ib()
    resolver_net = attr.ib()
    probe_info = attr.ib()
    edns0_subnet_info = attr.ib()
    qname_minimization = attr.ib(default=False)
    nxdomain_hijack = attr.ib(default=False)
    error = attr.ib(default=0)  # DNS rcode, 3842 = empty buf
    extra = attr.ib(default=None)
    dnssec_bogus_resolved = attr.ib(default=False)
    dnssec_valid_resolved = attr.ib(default=False)


    def pretty(self):
        return "[%s] [%s] ext: %s, ext_resolver: %s, edns0: %s qname-min: %s, nxdomain: %s extra: %s" % (
            self.from_probe,
            self.measurement_type,
            self.from_ip,
            self.external_resolvers,
            self.edns0_subnet_info,
            self.qname_minimization,
            self.nxdomain_hijack,
            self.extra
        )

    def merge(self, y):
        self.external_resolvers = self.external_resolvers.union(y.external_resolvers)
        self.resolver_asn = self.resolver_asn.union(y.resolver_asn)
        self.internal_resolvers = self.internal_resolvers.union(y.internal_resolvers)

        return self

def parse_result(results):
    for res in results:
        _LOGGER.debug("== Result ==\n%s", pf(res))
        if "resultset" in res:
            meas_type = MeasurementType(res["msm_id"])
            info = ResolverInfo(ts=res["timestamp"],
                                from_ip=res["from"],
                                from_probe=res["prb_id"],
                                internal_resolvers=None,
                                external_resolvers=None,
                                resolver_net=None,
                                resolver_asn=None,
                                probe_info=None,
                                measurement_type=meas_type,
                                edns0_subnet_info=None,
                                qname_minimization=False,
                                nxdomain_hijack=False,
                                error=0,
                                dnssec_bogus_resolved=False,
                                dnssec_valid_resolved=False,
                                )
            res_set = res["resultset"]
            from_probe = res["prb_id"]

            if "probe" in res:
                info.probe_info = res["probe"]
            else:
                info.probe_info = get_probe_info(from_probe)

            _LOGGER.debug("== Probe info ==\n%s", pf(info.probe_info))

            for res_measure in res_set:
                if "result" not in res_measure:
                    _LOGGER.debug("no results in measure: %s", res_measure)
                    if "error" in res_measure:
                        info.error = res_measure["error"]
                    yield info
                    continue

                #pp(res_measure)
                try:
                    dns_buf = dnslib.DNSRecord.parse(base64.b64decode(res_measure["result"]["abuf"]))
                except dnslib.dns.DNSError as ex:
                    _LOGGER.warning("Unable to parse abuf: %s" % ex)
                    info.rcode = 8044
                    info.extra = {'ex': str(ex)}
                    yield info
                    continue

                info.internal_resolvers = res_measure["dst_addr"]
                rcode = dns_buf.header.get_rcode()
                info.error = rcode
                if rcode != 0:
                    dns_error_msg = dnslib.dns.RCODE.get(rcode)
                    info.extra = {'error_string': dns_error_msg}
                    yield info
                    continue # hm, okay to reuse the object?

                if len(dns_buf.rr) < 1 or dns_buf.a.rdata is None:
                    _LOGGER.debug("got no rdata: %s", dns_buf)
                    info.error = 3842 # empty buf
                    #raise Exception()
                    # TODO gotta return non-true indicator here
                    continue


                elif meas_type == MeasurementType.qname_minim:
                    for rr in dns_buf.rr:
                        rr_s = str(rr.rdata).strip('"')
                        if rr_s.startswith("HOORAY"):  # qname min
                            info.qname_minimization = True
                elif meas_type == MeasurementType.akamai_whois\
                        or meas_type == MeasurementType.google_whois\
                        or meas_type == MeasurementType.ipv4_tcp\
                        or meas_type == MeasurementType.ipv6_cap\
                        or meas_type == MeasurementType.ipv6_tcp:
                    for rr in dns_buf.rr:
                        rr_s = str(rr.rdata).strip('"')
                        if rr_s.startswith("edns0-client-subnet"):
                            info.edns0_subnet_info = rr_s.split(" ")[1]
                        else:
                            try:
                                ip = ip_address(rr_s)
                                info.external_resolvers = str(ip)
                            except ValueError as ex:
                                _LOGGER.warning("got unknown rdata: %s" % rr_s)
                elif meas_type == MeasurementType.nxdomain_hijack:
                    if dns_buf.header.get_rcode() != 3:
                        info.nxdomain_hijack = True
                        info.extra = {'hijacks_to': str(dns_buf.a.rdata)}

                elif meas_type == MeasurementType.dnssec_bogus:
                    if dns_buf.header.get_rcode() != 0:
                        info.dnssec_bogus_resolved = True
                elif meas_type == MeasurementType.dnssec_reference:
                    for rr in dns_buf.rr:
                        rr_s = str(rr.rdata).strip('"')
                        if rr_s == "1.1.1.1":
                            info.dnssec_valid_resolved = True
                else:
                    _LOGGER.error("mtype not handled: %s", meas_type)

                #pp(dns_buf.a)
                if info.external_resolvers:
                    x = get_asn(info.external_resolvers)
                    if x is not None:
                        info.resolver_asn = x[0]
                        info.resolver_net = x[1]

                """
                <DNS RR: 'whoami.akamai.net.' rtype=A rclass=IN ttl=180 rdata='134.147.25.250'>
                <DNS RR: 'o-o.myaddr.l.google.com.' rtype=TXT rclass=IN ttl=60 rdata='"134.147.25.250"'>
                """

                yield info

def get_resolver_info(probe_ids, measurement):
    kwargs = {
        "msm_id": measurement,
        "start": datetime.utcnow() - timedelta(minutes=90),
        # "stop": datetime(2017, 4, 2),
    }
    if probe_ids:
    	kwargs["probe_ids"] = probe_ids
    is_success, results = AtlasResultsRequest(**kwargs).create()
    if not is_success:
        _LOGGER.error("Request failed: %s %s", probe_ids, measurement)
        return []

    return parse_result(results)


def get_info(probe):
    import itertools

    measurements = [get_resolver_info(probe, measure) for measure in MeasurementType.__members__.values() if do_all or measure in wanted_measurements]
    if measurements is None:
        _LOGGER.error("could not get any requested info")
        return
    for x in itertools.chain(*measurements):
        yield x
        #if x.from_probe in res:
        #    res[x.from_probe].merge(x)
        #else:
        #    res[x.from_probe] = x

    #return res

@click.group()
def cli():
    pass

@cli.command()
@click.option("--to", type=click.File('wb'), default=None)
def stored(to):
    q = [HOMEPROBE]  # , 1,2,3,4]
    q = None
    results = []
    for res in get_info(q):
        if res.dnssec_bogus_resolved:
            print(res.pretty())
        #if res.qname_minimization:
        #    print(res.pretty())
        #print(res.pretty())
        if to:
            results.append(attr.asdict(res))
    if to:
        to.write(json.dumps(results).encode('utf-8'))
        #pp(attr.asdict(res))

def got_result(results):
    for res in parse_result([results]):
        if res.dnssec_bogus_resolved:
            print(res.pretty())
        #pp(attr.asdict(result))
        continue


@cli.command()
def stream():
    stream = AtlasStream()
    stream.connect()

    try:
        stream.bind_channel("result", got_result)
        for meas in MeasurementType:
            #print(meas)
            #print(wanted_measurements)
            if not do_all and meas not in wanted_measurements:
                print("skipping")
                continue
            stream_parameters = {"msm": meas,
                                 "type": "dns",
                                 "enrichProbes": True,
                                 "sendBacklog": True,
                                 }
            stream.start_stream(stream_type="result", **stream_parameters)
            stream.timeout(5)

        while True:
            import time
            time.sleep(0.1)

    except Exception as ex:
        _LOGGER.warning("Got ex: %s" % ex)
        raise Exception() from ex
    finally:
        stream.disconnect()

if __name__ == "__main__":
    cli()
