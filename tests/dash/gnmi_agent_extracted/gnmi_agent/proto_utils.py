import base64
import re
import socket
import uuid
import importlib

from dash_api.appliance_pb2 import Appliance
from dash_api.eni_pb2 import Eni, State, EniMode  # noqa: F401
from dash_api.eni_route_pb2 import EniRoute
from dash_api.route_group_pb2 import RouteGroup
from dash_api.route_pb2 import Route
from dash_api.route_type_pb2 import ActionType, RouteType, RouteTypeItem, EncapType, RoutingType  # noqa: F401
from dash_api.vnet_mapping_pb2 import VnetMapping
from dash_api.vnet_pb2 import Vnet
from dash_api.meter_policy_pb2 import MeterPolicy
from dash_api.meter_rule_pb2 import MeterRule
from dash_api.meter_pb2 import Meter
from dash_api.tunnel_pb2 import Tunnel
from dash_api.route_rule_pb2 import RouteRule
from dash_api.outbound_port_map_pb2 import OutboundPortMap
from dash_api.outbound_port_map_range_pb2 import OutboundPortMapRange, PortMapRangeAction

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import ParseDict

ENABLE_PROTO = True

_DASH_TABLE_RE = re.compile(r"DASH_(\w+)_TABLE")
_ENUM_PARTS_RE = re.compile(r"[A-Z][^A-Z]*")

PB_INT_TYPES = set([
    FieldDescriptor.TYPE_INT32,
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_FIXED32,
    FieldDescriptor.TYPE_SFIXED32,
    FieldDescriptor.TYPE_SFIXED64,
    FieldDescriptor.TYPE_SINT32,
    FieldDescriptor.TYPE_SINT64
])

PB_CLASS_MAP = {
    "APPLIANCE": Appliance,
    "VNET": Vnet,
    "ENI": Eni,
    "VNET_MAPPING": VnetMapping,
    "ROUTE": Route,
    "ROUTING_TYPE": RouteType,
    "ROUTE_GROUP": RouteGroup,
    "ENI_ROUTE": EniRoute,
    "METER_POLICY": MeterPolicy,
    "METER_RULE": MeterRule,
    "TUNNEL": Tunnel,
    "ROUTE_RULE": RouteRule
}


def parse_ip_address(ip_str):
    if ":" in ip_str:
        packed = socket.inet_pton(socket.AF_INET6, ip_str)
        return {"ipv6": base64.b64encode(packed)}
    packed = socket.inet_pton(socket.AF_INET, ip_str)
    return {"ipv4": int.from_bytes(packed, "little")}


def parse_byte_field(orig_val):
    return base64.b64encode(bytes.fromhex(orig_val.replace(":", "")))


def parse_guid(guid_str):
    return {"value": parse_byte_field(uuid.UUID(guid_str).hex)}


def parse_value_or_range(orig):
    if isinstance(orig, list):
        if len(orig) == 1:
            val = int(orig[0])
            return {"value": val}
        elif len(orig) == 2:
            min = int(orig[0])
            max = int(orig[1])
            return {"range": {"min": min, "max": max}}
    else:
        val = int(orig)
        return {"value": val}


# ---------------------------------------------------------------------------
# Fast-path helpers for high-volume tables (VNET_MAPPING, ROUTE).
#
# These bypass ParseDict + per-field descriptor walks by writing proto fields
# directly. Falls back to the generic path for any other table.
# ---------------------------------------------------------------------------

# Cached enum string -> int lookups (get_enum_type_from_str has regex work).
_ENUM_VALUE_CACHE = {}


def _enum_val(enum_type_name, enum_name_str):
    cache_key = (enum_type_name, enum_name_str)
    v = _ENUM_VALUE_CACHE.get(cache_key)
    if v is None:
        v = get_enum_type_from_str(enum_type_name, enum_name_str)
        _ENUM_VALUE_CACHE[cache_key] = v
    return v


def _fill_ip(msg, s):
    """Populate an IpAddress proto sub-message in-place."""
    if ":" in s:
        msg.ipv6 = socket.inet_pton(socket.AF_INET6, s)
    else:
        msg.ipv4 = int.from_bytes(socket.inet_pton(socket.AF_INET, s), "little")


def _fill_prefix(msg, s):
    """Populate an IpPrefix proto sub-message in-place. Accepts /N or /<literal-mask>."""
    ip_str, mask = s.split("/", 1)
    _fill_ip(msg.ip, ip_str)
    if mask.isdigit():
        mask_str = prefix_to_ipv6(mask) if ":" in ip_str else prefix_to_ipv4(mask)
    else:
        mask_str = mask
    _fill_ip(msg.mask, mask_str)


# Route proto uses either "routing_type" or "action_type" depending on schema
# version. Detect once at module load and route both input keys to it.
_ROUTE_FIELDS = Route.DESCRIPTOR.fields_by_name
if "routing_type" in _ROUTE_FIELDS:
    _ROUTE_ENUM_FIELD = "routing_type"
elif "action_type" in _ROUTE_FIELDS:
    _ROUTE_ENUM_FIELD = "action_type"
else:
    _ROUTE_ENUM_FIELD = None
_ROUTE_ENUM_TYPE_NAME = (
    _ROUTE_FIELDS[_ROUTE_ENUM_FIELD].enum_type.name if _ROUTE_ENUM_FIELD else None
)


def _build_vnet_mapping_fast(d):
    """Hand-coded VnetMapping builder. Handles privatelink and vnet_encap shapes."""
    pb = VnetMapping()
    rt = d.get("routing_type")
    if rt is not None:
        pb.routing_type = rt if isinstance(rt, int) else _enum_val("RoutingType", rt)
    ip = d.get("underlay_ip")
    if ip is not None:
        _fill_ip(pb.underlay_ip, ip)
    sip = d.get("overlay_sip_prefix")
    if sip is not None:
        _fill_prefix(pb.overlay_sip_prefix, sip)
    dip = d.get("overlay_dip_prefix")
    if dip is not None:
        _fill_prefix(pb.overlay_dip_prefix, dip)
    mac = d.get("mac_address")
    if mac is not None:
        pb.mac_address = bytes.fromhex(mac.replace(":", ""))
    udv = d.get("use_dst_vni")
    if udv is not None:
        pb.use_dst_vni = udv is True or udv in ("true", "True", "TRUE")
    vni = d.get("vni")
    if vni is not None:
        pb.vni = int(vni)
    return pb


def _build_route_fast(d):
    """Hand-coded Route builder. Accepts both 'routing_type' and 'action_type' keys."""
    pb = Route()
    enum_val = d.get("routing_type")
    if enum_val is None:
        enum_val = d.get("action_type")
    if enum_val is not None and _ROUTE_ENUM_FIELD is not None:
        if isinstance(enum_val, int):
            setattr(pb, _ROUTE_ENUM_FIELD, enum_val)
        else:
            setattr(pb, _ROUTE_ENUM_FIELD, _enum_val(_ROUTE_ENUM_TYPE_NAME, enum_val))
    vnet = d.get("vnet")
    if vnet is not None:
        pb.vnet = vnet
    overlay_ip = d.get("overlay_ip")
    if overlay_ip is not None:
        _fill_ip(pb.overlay_ip, overlay_ip)
    destination = d.get("destination")
    if destination is not None:
        _fill_prefix(pb.destination, destination)
    priority = d.get("priority")
    if priority is not None:
        pb.priority = int(priority)
    return pb


def parse_dash_proto(key: str, proto_dict: dict):
    """
    Custom parser for DASH configs to allow writing configs
    in a more human-readable format
    """
    table_name = _DASH_TABLE_RE.search(key).group(1)

    # Fast paths for high-volume tables -- bypass ParseDict entirely.
    if table_name == "VNET_MAPPING":
        return _build_vnet_mapping_fast(proto_dict)
    if table_name == "ROUTE":
        return _build_route_fast(proto_dict)

    message = PB_CLASS_MAP[table_name]()
    field_map = message.DESCRIPTOR.fields_by_name

    if table_name == "ROUTING_TYPE":
        pb = routing_type_from_json(proto_dict)
        return pb

    new_dict = {}
    for key, value in proto_dict.items():
        if field_map[key].type == field_map[key].TYPE_MESSAGE:

            if field_map[key].message_type.name == "IpAddress":
                if field_map[key].label == FieldDescriptor.LABEL_REPEATED or isinstance(value,list):
                    new_dict[key] = [parse_ip_address(val) for val in value]
                else:
                    new_dict[key] = parse_ip_address(value)
            elif field_map[key].message_type.name == "IpPrefix":
                new_dict[key] = parse_ip_prefix(value)
            elif field_map[key].message_type.name == "Guid":
                new_dict[key] = parse_guid(value)
            elif field_map[key].message_type.name == "ValueOrRange":
                new_dict[key] = parse_value_or_range(value)

        elif field_map[key].type == field_map[key].TYPE_BYTES:
            new_dict[key] = parse_byte_field(value)

        elif field_map[key].type == field_map[key].TYPE_ENUM:
            if isinstance(value, int):
                new_dict[key] = value
            else:
                new_dict[key] = get_enum_type_from_str(field_map[key].enum_type.name, value)

        elif field_map[key].type in PB_INT_TYPES:
            new_dict[key] = int(value)

        if key not in new_dict:
            new_dict[key] = value

    return ParseDict(new_dict, message)


def get_enum_type_from_str(enum_type_str, enum_name_str):

    # 4_to_6 uses small cap so cannot use dynamic naming
    if enum_name_str == "4_to_6":
        return ActionType.ACTION_TYPE_4_to_6

    if enum_type_str == "EniMode":
        if enum_name_str == "floating_nic_mode":
            return EniMode.MODE_FNIC
        else:
            return EniMode.MODE_VM

    my_enum_type_parts = _ENUM_PARTS_RE.findall(enum_type_str)
    my_enum_type_concatenated = '_'.join(my_enum_type_parts)
    enum_name = f"{my_enum_type_concatenated.upper()}_{enum_name_str.upper()}"
    a = globals()[enum_type_str]
    if a is not None:
        """Returns the value for the given enum name and raisees ValueError if not found."""
        return a.Value(enum_name)
    else:
        raise Exception(f"Cannot find enum type {enum_type_str}")


def routing_type_from_json(json_obj):
    pb = RouteType()
    if isinstance(json_obj, list):
        for item in json_obj:
            pbi = RouteTypeItem()
            pbi.action_name = item["action_name"]
            pbi.action_type = get_enum_type_from_str('ActionType', item.get("action_type"))
            if item.get("encap_type") is not None:
                pbi.encap_type = get_enum_type_from_str('EncapType', item.get("encap_type"))
            if item.get("vni") is not None:
                pbi.vni = int(item["vni"])
            pb.items.append(pbi)
    else:
        pbi = RouteTypeItem()
        pbi.action_name = json_obj["action_name"]
        pbi.action_type = get_enum_type_from_str('ActionType', json_obj.get("action_type"))
        if json_obj.get("encap_type") is not None:
            pbi.encap_type = get_enum_type_from_str('EncapType', json_obj.get("encap_type"))
        if json_obj.get("vni") is not None:
            pbi.vni = int(json_obj["vni"])
        pb.items.append(pbi)
    return pb

def outbound_portmap_range_from_json(json_obj):
    pb = OutboundPortMapRange()
    if isinstance(json_obj, list):
        for item in json_obj:
            pbi = RouteTypeItem()
            pbi.action_name = item["action_name"]
            pbi.action_type = get_enum_type_from_str('ActionType', item.get("action_type"))
            if item.get("encap_type") is not None:
                pbi.encap_type = get_enum_type_from_str('EncapType', item.get("encap_type"))
            if item.get("vni") is not None:
                pbi.vni = int(item["vni"])
            pb.items.append(pbi)
    else:
        pbi = RouteTypeItem()
        pbi.action_name = json_obj["action_name"]
        pbi.action_type = get_enum_type_from_str('ActionType', json_obj.get("action_type"))
        if json_obj.get("encap_type") is not None:
            pbi.encap_type = get_enum_type_from_str('EncapType', json_obj.get("encap_type"))
        if json_obj.get("vni") is not None:
            pbi.vni = int(json_obj["vni"])
        pb.items.append(pbi)

def get_message_from_table_name(table_name):
    table_name_lis = table_name.lower().split("_")
    table_name_lis2 = [item.capitalize() for item in table_name_lis]
    message_name = ''.join(table_name_lis2)
    module_name = f'dash_api.{table_name.lower()}_pb2'

    # Import the module dynamically
    module = importlib.import_module(module_name)

    # Get the class object
    message_class = getattr(module, message_name)

    return message_class()


def prefix_to_ipv4(prefix_length):
    if int(prefix_length) > 32:
        return ""
    mask = 2**32 - 2**(32-int(prefix_length))
    s = str(hex(mask))
    s = s[2:]
    hex_groups = [s[i:i+2] for i in range(0, len(s), 2)]
    decimal_groups = []
    for hex_string in hex_groups:
        decimal_groups.append(str(int(hex_string, 16)))
    ipv4_address_str = '.'.join(decimal_groups)
    return ipv4_address_str


def prefix_to_ipv6(prefix_length):
    if int(prefix_length) > 128:
        return ""
    mask = 2**128 - 2**(128-int(prefix_length))
    s = str(hex(mask))
    s = s[2:]
    hex_groups = [s[i:i+4] for i in range(0, len(s), 4)]
    ipv6_address_str = ':'.join(hex_groups)
    return ipv6_address_str


def parse_ip_prefix(ip_prefix_str):
    ip_addr_str, mask = ip_prefix_str.split("/")
    if mask.isdigit():
        if ":" in ip_addr_str:
            mask_str = prefix_to_ipv6(mask)
        else:
            mask_str = prefix_to_ipv4(mask)
    else:
        mask_str = mask
    return {"ip": parse_ip_address(ip_addr_str), "mask": parse_ip_address(mask_str)}


def json_to_proto(key: str, proto_dict: dict):
    """
    Custom parser for DASH configs to allow writing configs
    in a more human-readable format
    """
    #import pdb;pdb.set_trace()
    table_name = _DASH_TABLE_RE.search(key).group(1)
    if table_name == "ROUTING_TYPE":
        pb = routing_type_from_json(proto_dict)
        return pb.SerializeToString()
    """
    if table_name == "OUTBOUND_PORT_MAP_RANGE":
        pb = outbound_portmap_range_from_json(proto_dict)
        return pb.SerializeToString()
    """

    message = get_message_from_table_name(table_name)
    field_map = message.DESCRIPTOR.fields_by_name
    new_dict = {}
    for key, value in proto_dict.items():
        #import pdb;pdb.set_trace()
        if field_map[key].type == field_map[key].TYPE_MESSAGE:

            if field_map[key].message_type.name == "IpAddress":
                if isinstance(value,list):
                    new_dict[key] = [parse_ip_address(val) for val in value]
                else:
                    new_dict[key] = parse_ip_address(value)
            elif field_map[key].message_type.name == "IpPrefix":
                new_dict[key] = parse_ip_prefix(value)
            elif field_map[key].message_type.name == "Guid":
                new_dict[key] = parse_guid(value)

        elif field_map[key].type == field_map[key].TYPE_ENUM:
            #new_dict[key] = get_enum_type_from_str(field_map[key].enum_type.name, value)
            if isinstance(value, int):
                new_dict[key] = value
            else:
                new_dict[key] = get_enum_type_from_str(field_map[key].enum_type.name, value)
        elif field_map[key].type == field_map[key].TYPE_BOOL:
            new_dict[key] = value == 'true'

        elif field_map[key].type == field_map[key].TYPE_BYTES:
            new_dict[key] = parse_byte_field(value)

        elif field_map[key].type in PB_INT_TYPES:
            new_dict[key] = int(value)

        if key not in new_dict:
            new_dict[key] = value

    pb = ParseDict(new_dict, message)
    return pb.SerializeToString()
