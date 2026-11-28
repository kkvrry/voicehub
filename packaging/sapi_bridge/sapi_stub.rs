// VoiceHub SAPI 原生桩：SAPI 自己加载引擎 DLL 并找 DllGetClassObject，
// 托管程序集没有这个导出，所以用这个极小的原生 DLL 顶上，转发给托管里的类工厂。
//
// 只做一件事：DllGetClassObject → CoCreateInstance(托管类工厂)。
// 真正的引擎实现在 VoiceHub.Sapi.dll（C#）里。

#![allow(non_snake_case)]

use std::ffi::c_void;

type HRESULT = i32;

/// 音色引擎的 CLSID：与注册表里语音令牌写的一致。
const VOICE_CLSID: [u8; 16] = [
    0x5E, 0x0A, 0x3C, 0x6F, // Data1 = 6F3C0A5E
    0x7B, 0x1C, // Data2 = 1C7B
    0x2E, 0x4D, // Data3 = 4D2E
    0x9E, 0x4B, 0x6A, 0x0D, 0x8F, 0x35, 0xC2, 0x11,
];

/// 托管类工厂的 CLSID：注册在 mscoree.dll 下。
const FACTORY_CLSID: [u8; 16] = [
    0x64, 0x7E, 0x9D, 0x2B, // Data1 = 2B9D7E64
    0x3A, 0x0F, // Data2 = 0F3A
    0x58, 0x4C, // Data3 = 4C58
    0x9D, 0x1E, 0x7A, 0x6C, 0x5B, 0x8E, 0x2F, 0x40,
];

const CLASS_E_CLASSNOTAVAILABLE: HRESULT = 0x80040111u32 as HRESULT;
const E_INVALIDARG: HRESULT = 0x80070057u32 as HRESULT;
const CLSCTX_INPROC_SERVER: u32 = 1;

#[link(name = "ole32")]
extern "system" {
    fn CoCreateInstance(
        rclsid: *const u8,
        punkouter: *mut c_void,
        dwclscontext: u32,
        riid: *const u8,
        ppv: *mut *mut c_void,
    ) -> HRESULT;
}

fn same_guid(left: *const u8, right: &[u8; 16]) -> bool {
    if left.is_null() {
        return false;
    }
    let mut index = 0usize;
    while index < 16 {
        if unsafe { *left.add(index) } != right[index] {
            return false;
        }
        index += 1;
    }
    true
}

/// COM 的类工厂入口：SAPI 加载引擎 DLL 后找的就是它。
#[no_mangle]
pub extern "system" fn DllGetClassObject(
    rclsid: *const u8,
    riid: *const u8,
    ppv: *mut *mut c_void,
) -> HRESULT {
    if ppv.is_null() {
        return E_INVALIDARG;
    }
    unsafe {
        *ppv = std::ptr::null_mut();
    }
    if !same_guid(rclsid, &VOICE_CLSID) {
        return CLASS_E_CLASSNOTAVAILABLE;
    }
    unsafe {
        CoCreateInstance(
            FACTORY_CLSID.as_ptr(),
            std::ptr::null_mut(),
            CLSCTX_INPROC_SERVER,
            riid,
            ppv,
        )
    }
}

/// SAPI 有时会问这个 DLL 能不能卸载。
#[no_mangle]
pub extern "system" fn DllCanUnloadNow() -> HRESULT {
    0
}
