# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: nic_simulator_grpc_service.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n nic_simulator_grpc_service.proto\"-\n\x0c\x41\x64minRequest\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\r\n\x05state\x18\x02 \x03(\x08\"+\n\nAdminReply\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\r\n\x05state\x18\x02 \x03(\x08\"\"\n\x10OperationRequest\x12\x0e\n\x06portid\x18\x01 \x03(\x05\"/\n\x0eOperationReply\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\r\n\x05state\x18\x02 \x03(\x08\"\"\n\x10LinkStateRequest\x12\x0e\n\x06portid\x18\x01 \x03(\x05\"/\n\x0eLinkStateReply\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\r\n\x05state\x18\x02 \x03(\x08\"\'\n\x14ServerVersionRequest\x12\x0f\n\x07version\x18\x01 \x01(\t\"%\n\x12ServerVersionReply\x12\x0f\n\x07version\x18\x01 \x01(\t\"A\n\x0b\x44ropRequest\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\x11\n\tdirection\x18\x02 \x03(\x05\x12\x0f\n\x07recover\x18\x03 \x01(\x08\",\n\tDropReply\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\x0f\n\x07success\x18\x02 \x03(\x08\"$\n\x12\x46lapCounterRequest\x12\x0e\n\x06portid\x18\x01 \x03(\x05\"1\n\x10\x46lapCounterReply\x12\x0e\n\x06portid\x18\x01 \x03(\x05\x12\r\n\x05\x66laps\x18\x02 \x03(\x05\x32\xeb\x03\n\rDualToRActive\x12=\n\x1dQueryAdminForwardingPortState\x12\r.AdminRequest\x1a\x0b.AdminReply\"\x00\x12;\n\x1bSetAdminForwardingPortState\x12\r.AdminRequest\x1a\x0b.AdminReply\"\x00\x12?\n\x17QueryOperationPortState\x12\x11.OperationRequest\x1a\x0f.OperationReply\"\x00\x12\x36\n\x0eQueryLinkState\x12\x11.LinkStateRequest\x1a\x0f.LinkStateReply\"\x00\x12\x42\n\x12QueryServerVersion\x12\x15.ServerVersionRequest\x1a\x13.ServerVersionReply\"\x00\x12%\n\x07SetDrop\x12\x0c.DropRequest\x1a\n.DropReply\"\x00\x12<\n\x10QueryFlapCounter\x12\x13.FlapCounterRequest\x1a\x11.FlapCounterReply\"\x00\x12<\n\x10ResetFlapCounter\x12\x13.FlapCounterRequest\x1a\x11.FlapCounterReply\"\x00\x62\x06proto3')           # noqa E501


_ADMINREQUEST = DESCRIPTOR.message_types_by_name['AdminRequest']
_ADMINREPLY = DESCRIPTOR.message_types_by_name['AdminReply']
_OPERATIONREQUEST = DESCRIPTOR.message_types_by_name['OperationRequest']
_OPERATIONREPLY = DESCRIPTOR.message_types_by_name['OperationReply']
_LINKSTATEREQUEST = DESCRIPTOR.message_types_by_name['LinkStateRequest']
_LINKSTATEREPLY = DESCRIPTOR.message_types_by_name['LinkStateReply']
_SERVERVERSIONREQUEST = DESCRIPTOR.message_types_by_name['ServerVersionRequest']
_SERVERVERSIONREPLY = DESCRIPTOR.message_types_by_name['ServerVersionReply']
_DROPREQUEST = DESCRIPTOR.message_types_by_name['DropRequest']
_DROPREPLY = DESCRIPTOR.message_types_by_name['DropReply']
_FLAPCOUNTERREQUEST = DESCRIPTOR.message_types_by_name['FlapCounterRequest']
_FLAPCOUNTERREPLY = DESCRIPTOR.message_types_by_name['FlapCounterReply']
AdminRequest = _reflection.GeneratedProtocolMessageType('AdminRequest', (_message.Message,), {
    'DESCRIPTOR': _ADMINREQUEST,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:AdminRequest)
})
_sym_db.RegisterMessage(AdminRequest)

AdminReply = _reflection.GeneratedProtocolMessageType('AdminReply', (_message.Message,), {
    'DESCRIPTOR': _ADMINREPLY,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:AdminReply)
})
_sym_db.RegisterMessage(AdminReply)

OperationRequest = _reflection.GeneratedProtocolMessageType('OperationRequest', (_message.Message,), {
    'DESCRIPTOR': _OPERATIONREQUEST,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:OperationRequest)
})
_sym_db.RegisterMessage(OperationRequest)

OperationReply = _reflection.GeneratedProtocolMessageType('OperationReply', (_message.Message,), {
    'DESCRIPTOR': _OPERATIONREPLY,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:OperationReply)
})
_sym_db.RegisterMessage(OperationReply)

LinkStateRequest = _reflection.GeneratedProtocolMessageType('LinkStateRequest', (_message.Message,), {
    'DESCRIPTOR': _LINKSTATEREQUEST,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:LinkStateRequest)
})
_sym_db.RegisterMessage(LinkStateRequest)

LinkStateReply = _reflection.GeneratedProtocolMessageType('LinkStateReply', (_message.Message,), {
    'DESCRIPTOR': _LINKSTATEREPLY,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:LinkStateReply)
})
_sym_db.RegisterMessage(LinkStateReply)

ServerVersionRequest = _reflection.GeneratedProtocolMessageType('ServerVersionRequest', (_message.Message,), {
    'DESCRIPTOR': _SERVERVERSIONREQUEST,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:ServerVersionRequest)
})
_sym_db.RegisterMessage(ServerVersionRequest)

ServerVersionReply = _reflection.GeneratedProtocolMessageType('ServerVersionReply', (_message.Message,), {
    'DESCRIPTOR': _SERVERVERSIONREPLY,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:ServerVersionReply)
})
_sym_db.RegisterMessage(ServerVersionReply)

DropRequest = _reflection.GeneratedProtocolMessageType('DropRequest', (_message.Message,), {
    'DESCRIPTOR': _DROPREQUEST,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:DropRequest)
})
_sym_db.RegisterMessage(DropRequest)

DropReply = _reflection.GeneratedProtocolMessageType('DropReply', (_message.Message,), {
    'DESCRIPTOR': _DROPREPLY,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:DropReply)
})
_sym_db.RegisterMessage(DropReply)

FlapCounterRequest = _reflection.GeneratedProtocolMessageType('FlapCounterRequest', (_message.Message,), {
    'DESCRIPTOR': _FLAPCOUNTERREQUEST,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:FlapCounterRequest)
})
_sym_db.RegisterMessage(FlapCounterRequest)

FlapCounterReply = _reflection.GeneratedProtocolMessageType('FlapCounterReply', (_message.Message,), {
    'DESCRIPTOR': _FLAPCOUNTERREPLY,
    '__module__': 'nic_simulator_grpc_service_pb2'
    # @@protoc_insertion_point(class_scope:FlapCounterReply)
})
_sym_db.RegisterMessage(FlapCounterReply)

_DUALTORACTIVE = DESCRIPTOR.services_by_name['DualToRActive']
if _descriptor._USE_C_DESCRIPTORS == False:                                         # noqa E712

    DESCRIPTOR._options = None
    _ADMINREQUEST._serialized_start = 36
    _ADMINREQUEST._serialized_end = 81
    _ADMINREPLY._serialized_start = 83
    _ADMINREPLY._serialized_end = 126
    _OPERATIONREQUEST._serialized_start = 128
    _OPERATIONREQUEST._serialized_end = 162
    _OPERATIONREPLY._serialized_start = 164
    _OPERATIONREPLY._serialized_end = 211
    _LINKSTATEREQUEST._serialized_start = 213
    _LINKSTATEREQUEST._serialized_end = 247
    _LINKSTATEREPLY._serialized_start = 249
    _LINKSTATEREPLY._serialized_end = 296
    _SERVERVERSIONREQUEST._serialized_start = 298
    _SERVERVERSIONREQUEST._serialized_end = 337
    _SERVERVERSIONREPLY._serialized_start = 339
    _SERVERVERSIONREPLY._serialized_end = 376
    _DROPREQUEST._serialized_start = 378
    _DROPREQUEST._serialized_end = 443
    _DROPREPLY._serialized_start = 445
    _DROPREPLY._serialized_end = 489
    _FLAPCOUNTERREQUEST._serialized_start = 491
    _FLAPCOUNTERREQUEST._serialized_end = 527
    _FLAPCOUNTERREPLY._serialized_start = 529
    _FLAPCOUNTERREPLY._serialized_end = 578
    _DUALTORACTIVE._serialized_start = 581
    _DUALTORACTIVE._serialized_end = 1072
# @@protoc_insertion_point(module_scope)
