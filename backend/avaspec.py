#!/usr/bin/env python3
import ctypes
import numpy as np

AVS_SERIAL_LEN = 10
USER_ID_LEN = 64
MAX_PIXELS = 4096

_lib = ctypes.CDLL("libavs.so")

class AvsIdentityType(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("SerialNumber", ctypes.c_char * AVS_SERIAL_LEN),
        ("UserFriendlyName", ctypes.c_char * USER_ID_LEN),
        ("Status", ctypes.c_char),
    ]

class MeasConfigType(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("m_StartPixel", ctypes.c_uint16),
        ("m_StopPixel", ctypes.c_uint16),
        ("m_IntegrationTime", ctypes.c_float),
        ("m_IntegrationDelay", ctypes.c_uint32),
        ("m_NrAverages", ctypes.c_uint32),
        ("m_CorDynDark_m_Enable", ctypes.c_uint8),
        ("m_CorDynDark_m_ForgetPercentage", ctypes.c_uint8),
        ("m_Smoothing_m_SmoothPix", ctypes.c_uint16),
        ("m_Smoothing_m_SmoothModel", ctypes.c_uint8),
        ("m_SaturationDetection", ctypes.c_uint8),
        ("m_Trigger_m_Mode", ctypes.c_uint8),
        ("m_Trigger_m_Source", ctypes.c_uint8),
        ("m_Trigger_m_SourceType", ctypes.c_uint8),
        ("m_Control_m_StrobeControl", ctypes.c_uint16),
        ("m_Control_m_LaserDelay", ctypes.c_uint32),
        ("m_Control_m_LaserWidth", ctypes.c_uint32),
        ("m_Control_m_LaserWaveLength", ctypes.c_float),
        ("m_Control_m_StoreToRam", ctypes.c_uint16),
    ]

# ================= DLL prototypes =================

_lib.AVS_Init.argtypes = [ctypes.c_int]
_lib.AVS_Init.restype = ctypes.c_int

_lib.AVS_GetList.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(AvsIdentityType),
]
_lib.AVS_GetList.restype = ctypes.c_int

_lib.AVS_Activate.argtypes = [ctypes.POINTER(AvsIdentityType)]
_lib.AVS_Activate.restype = ctypes.c_int

_lib.AVS_PrepareMeasure.argtypes = [ctypes.c_int, ctypes.POINTER(MeasConfigType)]
_lib.AVS_PrepareMeasure.restype = ctypes.c_int

_lib.AVS_Measure.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint16]
_lib.AVS_Measure.restype = ctypes.c_int

_lib.AVS_PollScan.argtypes = [ctypes.c_int]
_lib.AVS_PollScan.restype = ctypes.c_bool

_lib.AVS_GetScopeData.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_double * MAX_PIXELS),
]
_lib.AVS_GetScopeData.restype = ctypes.c_int

_lib.AVS_StopMeasure.argtypes = [ctypes.c_int]
_lib.AVS_Deactivate.argtypes = [ctypes.c_int]
_lib.AVS_Done.argtypes = []

_lib.AVS_UseHighResAdc.argtypes = [ctypes.c_int, ctypes.c_bool]
_lib.AVS_UseHighResAdc.restype = ctypes.c_int


# ================= API compatible =================

def AVS_Init(port=0):
    return _lib.AVS_Init(port)

def AVS_GetList(size=0, req=None, lst=None):
    required = ctypes.c_int(0)
    _lib.AVS_GetList(0, ctypes.byref(required), None)
    count = required.value // ctypes.sizeof(AvsIdentityType)
    ids = (AvsIdentityType * count)()
    _lib.AVS_GetList(required.value, ctypes.byref(required), ids)
    return count, ids

def AVS_Activate(dev):
    return _lib.AVS_Activate(ctypes.byref(dev))

def AVS_PrepareMeasure(handle, meas):
    return _lib.AVS_PrepareMeasure(handle, ctypes.byref(meas))

def AVS_Measure(handle, _, num):
    return _lib.AVS_Measure(handle, 0, num)

def AVS_PollScan(handle):
    return _lib.AVS_PollScan(handle)

def AVS_GetScopeData(handle, *_):
    ts = ctypes.c_uint32()
    spec = (ctypes.c_double * MAX_PIXELS)()
    ret = _lib.AVS_GetScopeData(handle, ctypes.byref(ts), ctypes.byref(spec))
    return ret, np.array(spec)

def AVS_StopMeasure(handle):
    return _lib.AVS_StopMeasure(handle)

def AVS_Deactivate(handle):
    return _lib.AVS_Deactivate(handle)

def AVS_Done():
    return _lib.AVS_Done()

def AVS_UseHighResAdc(handle, enable=True):
    """
    Enable / disable high resolution ADC
    """
    return _lib.AVS_UseHighResAdc(handle, enable)
