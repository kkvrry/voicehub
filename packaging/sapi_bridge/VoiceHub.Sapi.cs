// VoiceHub SAPI 引擎：把系统朗读转给 VoiceHub 的 /api/v1/tts，连不上时回落系统音色。
//
// 注册方式见同目录 install.ps1：COM 类 + SAPI 语音令牌。引擎在宿主进程内运行
// （Word、资源管理器、阅读 App 等），Speak 同步阻塞，所以网络往返必须有上限。
//
// 依据：Windows SDK sapiddk.h（ISpTTSEngine / ISpTTSEngineSite / SPVTEXTFRAG）与
// sapi.h（SPVSTATE / ISpObjectWithToken / ISpVoice / ISpObjectToken）。接口方法顺序
// 即 vtable 顺序，不要改。

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]
[assembly: AssemblyTitle("VoiceHub SAPI")]

namespace VoiceHub.Sapi
{
    #region 互操作

    [ComImport]
    [Guid("14056589-E16C-11D2-BB90-00C04F8EE6C0")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpObjectToken
    {
        // ISpDataKey
        [PreserveSig] int SetData(string pszValueName, uint cbData, IntPtr pData);
        [PreserveSig] int GetData(string pszValueName, out uint pcbData, IntPtr pData);
        [PreserveSig] int SetStringValue(string pszValueName, string pszValue);
        [PreserveSig] int GetStringValue(string pszValueName, out IntPtr ppszValue);
        [PreserveSig] int SetDWORD(string pszValueName, uint dwValue);
        [PreserveSig] int GetDWORD(string pszValueName, out uint pdwValue);
        [PreserveSig] int OpenKey(string pszSubKeyName, out IntPtr ppSubKey);
        [PreserveSig] int CreateKey(string pszSubKey, out IntPtr ppSubKey);
        [PreserveSig] int DeleteKey(string pszSubKey);
        [PreserveSig] int DeleteValue(string pszValueName);
        [PreserveSig] int EnumKeys(uint index, out IntPtr ppszSubKeyName);
        [PreserveSig] int EnumValues(uint index, out IntPtr ppszValueName);
        // ISpObjectToken
        [PreserveSig] int SetId(string pszCategoryId, string pszTokenId, int fCreateIfNotExist);
        [PreserveSig] int GetId(out IntPtr ppszCoMemTokenId);
        [PreserveSig] int GetCategory(out ISpObjectTokenCategory ppTokenCategory);
        [PreserveSig] int CreateInstance(IntPtr pUnkOuter, uint dwClsContext, ref Guid riid, out IntPtr ppvObject);
        [PreserveSig] int GetStorageFileName(IntPtr clsidCaller, string pszValueName, string pszFileNameSpecifier, uint nFolder, out IntPtr ppszFilePath);
        [PreserveSig] int RemoveStorageFileName(IntPtr clsidCaller, string pszKeyName, int fDeleteFile);
        [PreserveSig] int Remove(IntPtr pclsidCaller);
        [PreserveSig] int IsUISupported(string pszTypeOfUI, IntPtr pvExtraData, uint cbExtraData, IntPtr punkObject, out int pfSupported);
        [PreserveSig] int DisplayUI(IntPtr hwndParent, string pszTitle, string pszTypeOfUI, IntPtr pvExtraData, uint cbExtraData, IntPtr punkObject);
        [PreserveSig] int MatchesAttributes(string pszAttributes, out int pfMatches);
    }

    [ComImport]
    [Guid("2D3D3845-39AF-4850-BBF9-40B49780011D")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpObjectTokenCategory
    {
        // ISpDataKey
        [PreserveSig] int SetData(string pszValueName, uint cbData, IntPtr pData);
        [PreserveSig] int GetData(string pszValueName, out uint pcbData, IntPtr pData);
        [PreserveSig] int SetStringValue(string pszValueName, string pszValue);
        [PreserveSig] int GetStringValue(string pszValueName, out IntPtr ppszValue);
        [PreserveSig] int SetDWORD(string pszValueName, uint dwValue);
        [PreserveSig] int GetDWORD(string pszValueName, out uint pdwValue);
        [PreserveSig] int OpenKey(string pszSubKeyName, out IntPtr ppSubKey);
        [PreserveSig] int CreateKey(string pszSubKey, out IntPtr ppSubKey);
        [PreserveSig] int DeleteKey(string pszSubKey);
        [PreserveSig] int DeleteValue(string pszValueName);
        [PreserveSig] int EnumKeys(uint index, out IntPtr ppszSubKeyName);
        [PreserveSig] int EnumValues(uint index, out IntPtr ppszValueName);
        // ISpObjectTokenCategory
        [PreserveSig] int SetId(string pszCategoryId, int fCreateIfNotExist);
        [PreserveSig] int GetId(out IntPtr ppszCoMemCategoryId);
        [PreserveSig] int GetDataKey(int spdkl, out IntPtr ppDataKey);
        [PreserveSig] int EnumTokens(string pzsReqAttribs, string pszOptAttribs, out IEnumSpObjectTokens ppEnum);
        [PreserveSig] int SetDefaultTokenId(string pszTokenId);
        [PreserveSig] int GetDefaultTokenId(out IntPtr ppszCoMemTokenId);
    }

    [ComImport]
    [Guid("14056581-E16C-11D2-BB90-00C04F8EE628")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpDataKey
    {
        [PreserveSig] int SetData(string pszValueName, uint cbData, IntPtr pData);
        [PreserveSig] int GetData(string pszValueName, out uint pcbData, IntPtr pData);
        [PreserveSig] int SetStringValue(string pszValueName, string pszValue);
        [PreserveSig] int GetStringValue(string pszValueName, out IntPtr ppszValue);
        [PreserveSig] int SetDWORD(string pszValueName, uint dwValue);
        [PreserveSig] int GetDWORD(string pszValueName, out uint pdwValue);
        [PreserveSig] int OpenKey(string pszSubKeyName, out IntPtr ppSubKey);
        [PreserveSig] int CreateKey(string pszSubKey, out IntPtr ppSubKey);
        [PreserveSig] int DeleteKey(string pszSubKey);
        [PreserveSig] int DeleteValue(string pszValueName);
        [PreserveSig] int EnumKeys(uint index, out IntPtr ppszSubKeyName);
        [PreserveSig] int EnumValues(uint index, out IntPtr ppszValueName);
    }

    [ComImport]
    [Guid("06B64F9E-7FDA-11D2-B4F2-00C04F797396")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IEnumSpObjectTokens
    {
        [PreserveSig] int Next(uint celt, [MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 0)] IntPtr[] pelt, out uint pceltFetched);
        [PreserveSig] int Skip(uint celt);
        [PreserveSig] int Reset();
        [PreserveSig] int Clone(out IEnumSpObjectTokens ppEnum);
        [PreserveSig] int Item(uint index, out ISpObjectToken ppToken);
        [PreserveSig] int GetCount(out uint pCount);
    }

    [ComImport]
    [Guid("6C44DF74-72B9-4992-A1EC-EF996E0422D4")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpVoice
    {
        // ISpNotifySource
        [PreserveSig] int SetNotifySink(IntPtr pNotifySink);
        [PreserveSig] int SetNotifyWindowMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
        [PreserveSig] int SetNotifyCallbackFunction(IntPtr pfnCallback, IntPtr wParam, IntPtr lParam);
        [PreserveSig] int SetNotifyCallbackInterface(IntPtr pSpCallback, IntPtr wParam, IntPtr lParam);
        [PreserveSig] int SetNotifyWin32Event();
        [PreserveSig] int WaitForNotifyEvent(uint dwMilliseconds);
        [PreserveSig] IntPtr GetNotifyEventHandle();
        // ISpEventSource
        [PreserveSig] int SetInterest(IntPtr eventInterest, uint mask);
        [PreserveSig] int GetEvents(IntPtr pEvents, out uint pulEvents);
        [PreserveSig] int GetInfo(IntPtr pEventSourceInfo);
        // ISpVoice
        [PreserveSig] int SetOutput(IntPtr pUnkOutput, int fAllowFormatChanges);
        [PreserveSig] int GetOutputObjectToken(out ISpObjectToken ppObjectToken);
        [PreserveSig] int GetOutputStream(out IntPtr ppStream);
        [PreserveSig] int Pause();
        [PreserveSig] int Resume();
        [PreserveSig] int SetVoice(ISpObjectToken pToken);
        [PreserveSig] int GetVoice(out ISpObjectToken ppToken);
        [PreserveSig] int Speak(string pwcs, uint dwFlags, out uint pulStreamNumber);
        [PreserveSig] int SpeakStream(IntPtr pStream, uint dwFlags, out uint pulStreamNumber);
        [PreserveSig] int GetStatus(IntPtr pStatus, out IntPtr ppszLastBookmark);
        [PreserveSig] int Skip(string pItemType, int lNumItems, out uint pulNumSkipped);
        [PreserveSig] int SetPriority(int ePriority);
        [PreserveSig] int GetPriority(out int pePriority);
        [PreserveSig] int SetAlertBoundary(ushort eBoundary);
        [PreserveSig] int GetAlertBoundary(out ushort peBoundary);
        [PreserveSig] int SetRate(int RateAdjust);
        [PreserveSig] int GetRate(out int pRateAdjust);
        [PreserveSig] int SetVolume(ushort usVolume);
        [PreserveSig] int GetVolume(out ushort pusVolume);
        [PreserveSig] int WaitUntilDone(uint msTimeout, uint dwFlags);
        [PreserveSig] int SetSyncSpeakTimeout(uint dwTimeout);
        [PreserveSig] int GetSyncSpeakTimeout(out uint pdwTimeout);
        [PreserveSig] IntPtr SpeakCompleteEvent();
        [PreserveSig] int IsUISupported(string pszTypeOfUI, IntPtr pvExtraData, uint cbExtraData, IntPtr punkObject, out int pfSupported);
        [PreserveSig] int DisplayUI(uint ulstreamNumber, string pszTitle, string pszTypeOfUI, IntPtr pvExtraData, uint cbExtraData);
    }

    [ComImport]
    [Guid("A74D7C8E-4CC5-4F2F-A6EB-804DEE18500E")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpTTSEngine
    {
        [PreserveSig]
        int Speak(
            uint dwSpeakFlags,
            ref Guid rguidFormatId,
            IntPtr pWaveFormatEx,
            IntPtr pTextFragList,
            ISpTTSEngineSite pOutputSite);

        [PreserveSig]
        int GetOutputFormat(
            IntPtr pTargetFmtId,
            IntPtr pTargetWaveFormatEx,
            IntPtr pOutputFormatId,
            out IntPtr ppCoMemOutputWaveFormatEx);
    }

    [ComImport]
    [Guid("9880499B-CCE9-11D2-B503-00C04F797396")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpTTSEngineSite
    {
        [PreserveSig]
        int AddEvents(IntPtr pEventArray, uint ulCount);

        [PreserveSig]
        int GetEventInterest(out ulong pullEventInterest);

        [PreserveSig]
        uint GetActions();

        [PreserveSig]
        int Write(IntPtr pBuff, uint cb, out uint pcbWritten);

        [PreserveSig]
        int GetRate(out int pRateAdjust);

        [PreserveSig]
        int GetVolume(out ushort pusVolume);

        [PreserveSig]
        int GetSkipInfo(out int peType, out int plNumItems);

        [PreserveSig]
        int CompleteSkip(int ulNumSkipped);
    }

    [ComImport]
    [Guid("5B559F40-E952-11D2-BB91-00C04F8EE6C0")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpObjectWithToken
    {
        [PreserveSig]
        int SetObjectToken(IntPtr pToken);

        [PreserveSig]
        int GetObjectToken(out IntPtr ppToken);
    }

    [ComImport]
    [Guid("5EFF4AEF-8487-11D2-961C-00C04F8EE628")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpNotifySource
    {
        [PreserveSig] int SetNotifySink(IntPtr pNotifySink);
        [PreserveSig] int SetNotifyWindowMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
        [PreserveSig] int SetNotifyCallbackFunction(IntPtr pfnCallback, IntPtr wParam, IntPtr lParam);
        [PreserveSig] int SetNotifyCallbackInterface(IntPtr pSpCallback, IntPtr wParam, IntPtr lParam);
        [PreserveSig] int SetNotifyWin32Event();
        [PreserveSig] int WaitForNotifyEvent(uint dwMilliseconds);
        [PreserveSig] IntPtr GetNotifyEventHandle();
    }

    [ComImport]
    [Guid("12E3CCA9-7518-44C5-A5E7-BA5A79CB929E")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpStream
    {
        // IStream
        [PreserveSig] int Read(IntPtr pv, uint cb, out uint pcbRead);
        [PreserveSig] int Write(IntPtr pv, uint cb, out uint pcbWritten);
        [PreserveSig] int Seek(long dlibMove, int dwOrigin, out long plibNewPosition);
        [PreserveSig] int SetSize(ulong libNewSize);
        [PreserveSig] int CopyTo(IntPtr pstm, ulong cb, out ulong pcbRead, out ulong pcbWritten);
        [PreserveSig] int Commit(uint grfCommitFlags);
        [PreserveSig] int Revert();
        [PreserveSig] int LockRegion(ulong libOffset, ulong libCount, uint dwLockType);
        [PreserveSig] int UnlockRegion(ulong libOffset, ulong libCount, uint dwLockType);
        [PreserveSig] int Stat(IntPtr pstatstg, uint grfStatFlag);
        [PreserveSig] int Clone(out IntPtr ppstm);
        // ISpStreamFormat
        [PreserveSig] int GetFormat(IntPtr pguidFormatId, out IntPtr ppCoMemWaveFormatEx);
        // ISpStream
        [PreserveSig] int SetBaseStream(IntPtr pStream, int fReleasePrevious);
        [PreserveSig] int GetBaseStream(out IntPtr ppStream);
        [PreserveSig] int BindToFile(string pszFileName, int eMode, IntPtr pFormatId,
            IntPtr pWaveFormatEx, uint dwFlags);
        [PreserveSig] int Close();
    }

    [ComImport]
    [Guid("BED530BE-2606-4F4D-A1C0-54C5CDA5566F")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISpStreamFormat
    {
        // IStream
        [PreserveSig] int Read(IntPtr pv, uint cb, out uint pcbRead);
        [PreserveSig] int Write(IntPtr pv, uint cb, out uint pcbWritten);
        [PreserveSig] int Seek(long dlibMove, int dwOrigin, out long plibNewPosition);
        [PreserveSig] int SetSize(ulong libNewSize);
        [PreserveSig] int CopyTo(IntPtr pstm, ulong cb, out ulong pcbRead, out ulong pcbWritten);
        [PreserveSig] int Commit(uint grfCommitFlags);
        [PreserveSig] int Revert();
        [PreserveSig] int LockRegion(ulong libOffset, ulong libCount, uint dwLockType);
        [PreserveSig] int UnlockRegion(ulong libOffset, ulong libCount, uint dwLockType);
        [PreserveSig] int Stat(IntPtr pstatstg, uint grfStatFlag);
        [PreserveSig] int Clone(out IntPtr ppstm);
        // ISpStreamFormat
        [PreserveSig]
        int GetFormat(IntPtr pguidFormatId, out IntPtr ppCoMemWaveFormatEx);
    }

    [ComImport]
    [Guid("00000001-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IClassFactory
    {
        [PreserveSig]
        int CreateInstance(IntPtr pUnkOuter, ref Guid riid, out IntPtr ppvObject);

        [PreserveSig]
        int LockServer(int fLockServer);
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct WAVEFORMATEX
    {
        public ushort wFormatTag;
        public ushort nChannels;
        public uint nSamplesPerSec;
        public uint nAvgBytesPerSec;
        public ushort nBlockAlign;
        public ushort wBitsPerSample;
        public ushort cbSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SPVPITCH
    {
        public int MiddleAdj;
        public int RangeAdj;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SPVCONTEXT
    {
        public IntPtr pCategory;
        public IntPtr pBefore;
        public IntPtr pAfter;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SPVSTATE
    {
        public int eAction;
        public ushort LangID;
        public ushort wReserved;
        public int EmphAdj;
        public int RateAdj;
        public uint Volume;
        public SPVPITCH PitchAdj;
        public uint SilenceMSecs;
        public IntPtr pPhoneIds;
        public int ePartOfSpeech;
        public SPVCONTEXT Context;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SPVTEXTFRAG
    {
        public IntPtr pNext;
        public SPVSTATE State;
        public IntPtr pTextStart;
        public uint ulTextLen;
        public uint ulTextSrcOffset;
    }

    #endregion

    /// <summary>引擎配置。来源：%LOCALAPPDATA%\voicehub\sapi\bridge.json。</summary>
    internal sealed class BridgeConfig
    {
        public string Endpoint = "https://127.0.0.1:17865";
        public string Engine = "auto";
        public string VoiceId = "";
        public string Format = "wav";
        public int TimeoutSeconds = 30;
        public string FallbackVoice = "Microsoft Huihui Desktop";
        public string VoiceName = "VoiceHub TTS";
        public string Language = "804";

        /// <summary>配置文件路径：环境变量优先，其次本机数据目录。</summary>
        public static string ConfigPath
        {
            get
            {
                string fromEnv = Environment.GetEnvironmentVariable("VOICEHUB_SAPI_CONFIG");
                if (!string.IsNullOrEmpty(fromEnv))
                {
                    return fromEnv;
                }
                string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(local, "voicehub", "sapi", "bridge.json");
            }
        }

        public static string LogPath
        {
            get
            {
                string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(local, "voicehub", "sapi", "bridge.log");
            }
        }

        public static BridgeConfig Load()
        {
            var config = new BridgeConfig();
            try
            {
                string path = ConfigPath;
                if (File.Exists(path))
                {
                    var parsed = new JavaScriptSerializer()
                        .Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
                    config.Endpoint = Str(parsed, "endpoint", config.Endpoint);
                    config.Engine = Str(parsed, "engine", config.Engine);
                    config.VoiceId = Str(parsed, "voice_id", config.VoiceId);
                    config.Format = Str(parsed, "format", config.Format);
                    config.TimeoutSeconds = Int(parsed, "timeout_seconds", config.TimeoutSeconds);
                    config.FallbackVoice = Str(parsed, "fallback_voice", config.FallbackVoice);
                    config.VoiceName = Str(parsed, "voice_name", config.VoiceName);
                    config.Language = Str(parsed, "language", config.Language);
                }
            }
            catch (Exception)
            {
                // 配置读不到就用默认值：宁可退回系统音色，也不要让引擎直接失败。
            }
            return config;
        }

        private static string Str(Dictionary<string, object> map, string key, string fallback)
        {
            object value;
            if (map != null && map.TryGetValue(key, out value) && value != null)
            {
                string text = Convert.ToString(value, CultureInfo.InvariantCulture);
                if (!string.IsNullOrEmpty(text))
                {
                    return text;
                }
            }
            return fallback;
        }

        private static int Int(Dictionary<string, object> map, string key, int fallback)
        {
            object value;
            if (map != null && map.TryGetValue(key, out value) && value != null)
            {
                int parsed;
                if (int.TryParse(Convert.ToString(value, CultureInfo.InvariantCulture),
                        NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed)
                    && parsed > 0)
                {
                    return parsed;
                }
            }
            return fallback;
        }
    }

    internal static class Log
    {
        private static readonly object Gate = new object();

        public static void Write(string message)
        {
            lock (Gate)
            {
                try
                {
                    string path = BridgeConfig.LogPath;
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    if (File.Exists(path) && new FileInfo(path).Length > 512 * 1024)
                    {
                        File.Delete(path);
                    }
                    File.AppendAllText(
                        path,
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
                            + " " + message + Environment.NewLine,
                        Encoding.UTF8);
                }
                catch (Exception)
                {
                    // 日志写不进去不该影响朗读。
                }
            }
        }
    }

    /// <summary>项目接口的调用：自己开一条连接，不改宿主进程的全局设置。</summary>
    internal static class TtsClient
    {
        public static byte[] Synthesize(BridgeConfig config, string text)
        {
            Uri uri = new Uri(config.Endpoint.TrimEnd('/') + "/api/v1/tts");
            string body = "{\"text\":" + Quote(text)
                          + ",\"engine\":" + Quote(config.Engine)
                          + ",\"voice_id\":" + Quote(config.VoiceId)
                          + ",\"format\":" + Quote(config.Format) + "}";
            byte[] payload = Encoding.UTF8.GetBytes(body);

            using (var client = new TcpClient())
            {
                IAsyncResult connecting = client.BeginConnect(uri.Host, uri.Port, null, null);
                if (!connecting.AsyncWaitHandle.WaitOne(config.TimeoutSeconds * 1000))
                {
                    throw new TimeoutException("连不上 " + uri.Host + ":" + uri.Port);
                }
                client.EndConnect(connecting);

                using (Stream raw = client.GetStream())
                {
                    Stream stream = raw;
                    SslStream tls = null;
                    try
                    {
                        if (string.Equals(uri.Scheme, "https", StringComparison.OrdinalIgnoreCase))
                        {
                            tls = new SslStream(raw, false, AcceptCertificateFor(uri.Host));
                            tls.AuthenticateAsClient(uri.Host, null, SslProtocols.Tls12, false);
                            stream = tls;
                        }
                        stream.ReadTimeout = config.TimeoutSeconds * 1000;
                        stream.WriteTimeout = config.TimeoutSeconds * 1000;

                        string head = "POST " + uri.PathAndQuery + " HTTP/1.1\r\n"
                            + "Host: " + uri.Host + ":" + uri.Port + "\r\n"
                            + "User-Agent: VoiceHub-Sapi\r\n"
                            + "Accept: */*\r\n"
                            + "Content-Type: application/json; charset=utf-8\r\n"
                            + "Content-Length: " + payload.Length + "\r\n"
                            + "Connection: close\r\n\r\n";
                        byte[] headBytes = Encoding.ASCII.GetBytes(head);
                        stream.Write(headBytes, 0, headBytes.Length);
                        stream.Write(payload, 0, payload.Length);
                        stream.Flush();

                        return ReadResponse(stream);
                    }
                    finally
                    {
                        if (tls != null)
                        {
                            tls.Dispose();
                        }
                    }
                }
            }
        }

        /// <summary>读一条 HTTP/1.1 响应，返回响应体（音频）。</summary>
        private static byte[] ReadResponse(Stream stream)
        {
            var header = new MemoryStream();
            var line = new List<byte>();
            int matched = 0;
            while (true)
            {
                int value = stream.ReadByte();
                if (value < 0)
                {
                    throw new IOException("响应提前结束。");
                }
                header.WriteByte((byte)value);
                line.Add((byte)value);
                // 找 \r\n\r\n
                if (value == 0x0A)
                {
                    matched++;
                    if (matched == 2)
                    {
                        break;
                    }
                }
                else if (value != 0x0D)
                {
                    matched = 0;
                }
                if (line.Count > 65536)
                {
                    throw new IOException("响应头过大。");
                }
            }
            string text = Encoding.ASCII.GetString(header.ToArray());
            string[] lines = text.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0 || !lines[0].Contains(" 200"))
            {
                throw new IOException("接口返回：" + (lines.Length > 0 ? lines[0] : "空"));
            }
            long length = -1;
            bool chunked = false;
            foreach (string item in lines)
            {
                int colon = item.IndexOf(':');
                if (colon <= 0)
                {
                    continue;
                }
                string name = item.Substring(0, colon).Trim().ToLowerInvariant();
                string value = item.Substring(colon + 1).Trim();
                if (name == "content-length")
                {
                    long parsed;
                    if (long.TryParse(value, out parsed))
                    {
                        length = parsed;
                    }
                }
                else if (name == "transfer-encoding" && value.ToLowerInvariant().Contains("chunked"))
                {
                    chunked = true;
                }
            }
            if (chunked)
            {
                return ReadChunked(stream);
            }
            if (length < 0)
            {
                var rest = new MemoryStream();
                var temp = new byte[16384];
                int n;
                while ((n = stream.Read(temp, 0, temp.Length)) > 0)
                {
                    rest.Write(temp, 0, n);
                }
                return rest.ToArray();
            }
            var buffer = new byte[length];
            int offset = 0;
            while (offset < buffer.Length)
            {
                int read = stream.Read(buffer, offset, buffer.Length - offset);
                if (read <= 0)
                {
                    break;
                }
                offset += read;
            }
            if (offset != buffer.Length)
            {
                throw new IOException("响应体不完整。");
            }
            return buffer;
        }

        private static byte[] ReadChunked(Stream stream)
        {
            var result = new MemoryStream();
            while (true)
            {
                string sizeLine = ReadLine(stream);
                int semicolon = sizeLine.IndexOf(';');
                if (semicolon >= 0)
                {
                    sizeLine = sizeLine.Substring(0, semicolon);
                }
                int size;
                if (!int.TryParse(sizeLine.Trim(), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out size) || size <= 0)
                {
                    break;
                }
                var chunk = new byte[size];
                int offset = 0;
                while (offset < size)
                {
                    int read = stream.Read(chunk, offset, size - offset);
                    if (read <= 0)
                    {
                        break;
                    }
                    offset += read;
                }
                result.Write(chunk, 0, offset);
                ReadLine(stream);
            }
            return result.ToArray();
        }

        private static string ReadLine(Stream stream)
        {
            var line = new List<byte>();
            while (true)
            {
                int value = stream.ReadByte();
                if (value < 0 || value == 0x0A)
                {
                    break;
                }
                if (value != 0x0D)
                {
                    line.Add((byte)value);
                }
            }
            return Encoding.ASCII.GetString(line.ToArray());
        }

        /// <summary>本机接口用的是现签证书，只对回环地址放行。</summary>
        private static RemoteCertificateValidationCallback AcceptCertificateFor(string host)
        {
            return delegate (object sender, X509Certificate certificate, X509Chain chain,
                SslPolicyErrors errors)
            {
                IPAddress address;
                if (IPAddress.TryParse(host, out address))
                {
                    if (IPAddress.IsLoopback(address))
                    {
                        return true;
                    }
                }
                else if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
                return errors == SslPolicyErrors.None;
            };
        }

        private static string Quote(string value)
        {
            if (value == null)
            {
                value = string.Empty;
            }
            var builder = new StringBuilder(value.Length + 8);
            builder.Append('"');
            foreach (char c in value)
            {
                switch (c)
                {
                    case '"': builder.Append("\\\""); break;
                    case '\\': builder.Append("\\\\"); break;
                    case '\n': builder.Append("\\n"); break;
                    case '\r': builder.Append("\\r"); break;
                    case '\t': builder.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                        {
                            builder.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            builder.Append(c);
                        }
                        break;
                }
            }
            builder.Append('"');
            return builder.ToString();
        }
    }

    /// <summary>WAV 解码与格式转换。</summary>
    internal static class Pcm
    {
        public sealed class Format
        {
            public int Channels;
            public int SampleRate;
            public int BitsPerSample;
        }

        /// <summary>只认 16bit PCM：项目侧现在给的就是这个。</summary>
        public static bool TryDecodeWav(byte[] data, out Format format, out short[] samples)
        {
            format = null;
            samples = null;
            if (data == null || data.Length < 44)
            {
                return false;
            }
            if (data[0] != 'R' || data[1] != 'I' || data[2] != 'F' || data[3] != 'F'
                || data[8] != 'W' || data[9] != 'A' || data[10] != 'V' || data[11] != 'E')
            {
                return false;
            }
            int channels = 0;
            int sampleRate = 0;
            int bits = 0;
            int audioFormat = 0;
            int offset = 12;
            int dataOffset = -1;
            int dataLength = 0;
            while (offset + 8 <= data.Length)
            {
                string chunk = Encoding.ASCII.GetString(data, offset, 4);
                int size = BitConverter.ToInt32(data, offset + 4);
                int body = offset + 8;
                if (size < 0 || body + size > data.Length)
                {
                    break;
                }
                if (chunk == "fmt ")
                {
                    audioFormat = BitConverter.ToInt16(data, body);
                    channels = BitConverter.ToInt16(data, body + 2);
                    sampleRate = BitConverter.ToInt32(data, body + 4);
                    bits = BitConverter.ToInt16(data, body + 14);
                }
                else if (chunk == "data")
                {
                    dataOffset = body;
                    dataLength = size;
                }
                offset = body + size + (size % 2);
            }
            if (audioFormat != 1 || bits != 16 || channels <= 0 || sampleRate <= 0 || dataOffset < 0)
            {
                return false;
            }
            int count = Math.Min(dataLength, data.Length - dataOffset) / 2;
            var buffer = new short[count];
            Buffer.BlockCopy(data, dataOffset, buffer, 0, count * 2);
            format = new Format
            {
                Channels = channels,
                SampleRate = sampleRate,
                BitsPerSample = bits,
            };
            samples = buffer;
            return true;
        }

        /// <summary>转成 SAPI 要的格式：声道、采样率、位深。</summary>
        public static byte[] Convert(short[] samples, Format source, WAVEFORMATEX target)
        {
            int channels = target.nChannels > 0 ? target.nChannels : 1;
            int rate = target.nSamplesPerSec > 0 ? (int)target.nSamplesPerSec : source.SampleRate;
            int bits = target.wBitsPerSample > 0 ? target.wBitsPerSample : 16;

            short[] mono = ToMono(samples, source.Channels);
            short[] resampled = Resample(mono, source.SampleRate, rate);
            short[] spread = FromMono(resampled, channels);
            return ToBytes(spread, bits);
        }

        private static short[] ToMono(short[] samples, int channels)
        {
            if (channels <= 1)
            {
                return samples;
            }
            int frames = samples.Length / channels;
            var result = new short[frames];
            for (int i = 0; i < frames; i++)
            {
                int sum = 0;
                for (int c = 0; c < channels; c++)
                {
                    sum += samples[i * channels + c];
                }
                result[i] = (short)(sum / channels);
            }
            return result;
        }

        private static short[] FromMono(short[] samples, int channels)
        {
            if (channels <= 1)
            {
                return samples;
            }
            var result = new short[samples.Length * channels];
            for (int i = 0; i < samples.Length; i++)
            {
                for (int c = 0; c < channels; c++)
                {
                    result[i * channels + c] = samples[i];
                }
            }
            return result;
        }

        private static short[] Resample(short[] samples, int sourceRate, int targetRate)
        {
            if (sourceRate == targetRate || samples.Length == 0)
            {
                return samples;
            }
            int length = (int)((long)samples.Length * targetRate / sourceRate);
            if (length <= 0)
            {
                return new short[0];
            }
            var result = new short[length];
            double step = (double)samples.Length / length;
            for (int i = 0; i < length; i++)
            {
                double position = i * step;
                int left = (int)position;
                int right = Math.Min(left + 1, samples.Length - 1);
                double fraction = position - left;
                result[i] = (short)Math.Round(
                    samples[left] * (1 - fraction) + samples[right] * fraction,
                    MidpointRounding.AwayFromZero);
            }
            return result;
        }

        private static byte[] ToBytes(short[] samples, int bits)
        {
            if (bits == 16)
            {
                var buffer = new byte[samples.Length * 2];
                Buffer.BlockCopy(samples, 0, buffer, 0, buffer.Length);
                return buffer;
            }
            if (bits == 8)
            {
                var buffer = new byte[samples.Length];
                for (int i = 0; i < samples.Length; i++)
                {
                    buffer[i] = (byte)((samples[i] >> 8) + 128);
                }
                return buffer;
            }
            throw new NotSupportedException("位深 " + bits + " 不支持");
        }
    }

    /// <summary>给安装脚本与自检用的入口：列音色、设默认、试读。</summary>
    [ComVisible(false)]
    public static class Bridge
    {
        /// <summary>系统里可用的音色：令牌 id → 显示名。</summary>
        public static Dictionary<string, string> Voices()
        {
            return SystemVoices.List();
        }

        /// <summary>当前默认音色的令牌 id；取不到返回空串。</summary>
        public static string DefaultVoiceId()
        {
            ISpObjectTokenCategory category = SystemVoices.GetCategory();
            if (category == null)
            {
                return string.Empty;
            }
            try
            {
                IntPtr text;
                if (category.GetDefaultTokenId(out text) != 0 || text == IntPtr.Zero)
                {
                    return string.Empty;
                }
                string id = Marshal.PtrToStringUni(text);
                Marshal.FreeCoTaskMem(text);
                return id;
            }
            catch (Exception)
            {
                return string.Empty;
            }
        }

        /// <summary>把默认音色改成指定的令牌 id。</summary>
        public static bool SetDefaultVoiceId(string tokenId)
        {
            ISpObjectTokenCategory category = SystemVoices.GetCategory();
            if (category == null)
            {
                return false;
            }
            return category.SetDefaultTokenId(tokenId) == 0;
        }

        /// <summary>用指定音色念一句话；令牌 id 为空时用默认音色。</summary>
        public static bool Speak(string tokenId, string text)
        {
            return SystemVoices.Speak(tokenId, text);
        }

        /// <summary>用指定音色渲染到 WAV 文件（自检用，不占音频设备）。</summary>
        public static bool SpeakToFile(string tokenId, string text, string path)
        {
            short[] samples;
            Pcm.Format format;
            if (!SystemVoices.Render(tokenId, text, out samples, out format))
            {
                return false;
            }
            using (var writer = new BinaryWriter(System.IO.File.Create(path)))
            {
                writer.Write(Encoding.ASCII.GetBytes("RIFF"));
                writer.Write(36 + samples.Length * 2);
                writer.Write(Encoding.ASCII.GetBytes("WAVEfmt "));
                writer.Write(16);
                writer.Write((short)1);
                writer.Write((short)format.Channels);
                writer.Write(format.SampleRate);
                writer.Write(format.SampleRate * format.Channels * format.BitsPerSample / 8);
                writer.Write((short)(format.Channels * format.BitsPerSample / 8));
                writer.Write((short)format.BitsPerSample);
                writer.Write(Encoding.ASCII.GetBytes("data"));
                writer.Write(samples.Length * 2);
                foreach (short sample in samples)
                {
                    writer.Write(sample);
                }
            }
            return true;
        }
    }

    /// <summary>系统音色的读取与朗读（回落用）。</summary>
    internal static class SystemVoices
    {
        private static readonly Guid ClsidSpVoice = new Guid("96749377-3391-11D2-9EE3-00C04F797396");

        /// <summary>列出系统里可用的音色：id → 显示名。</summary>
        public static Dictionary<string, string> List()
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            ISpObjectTokenCategory category = null;
            IEnumSpObjectTokens enumerator = null;
            try
            {
                category = GetCategory();
                if (category == null)
                {
                    return result;
                }
                if (category.EnumTokens(null, null, out enumerator) != 0 || enumerator == null)
                {
                    return result;
                }
                var tokens = new IntPtr[1];
                uint fetched;
                int hr = enumerator.Next(1, tokens, out fetched);
                while (hr == 0 && fetched > 0)
                {
                    var token = Marshal.GetObjectForIUnknown(tokens[0]) as ISpObjectToken;
                    if (token != null)
                    {
                        string id = TokenId(token);
                        string name = TokenName(token);
                        if (!string.IsNullOrEmpty(id))
                        {
                            result[id] = name;
                        }
                        Marshal.ReleaseComObject(token);
                    }
                    Marshal.Release(tokens[0]);
                    hr = enumerator.Next(1, tokens, out fetched);
                }
            }
            catch (Exception ex)
            {
                Log.Write("枚举系统音色失败：" + ex.Message);
            }
            finally
            {
                if (enumerator != null) { Marshal.ReleaseComObject(enumerator); }
                if (category != null) { Marshal.ReleaseComObject(category); }
            }
            return result;
        }

        /// <summary>
        /// 让系统音色念一段话并把音频取回来。
        ///
        /// 不用音频设备：SAPI 正在用设备时（就是本引擎被调用的时候），
        /// 再开一路设备会返回 SPERR_DEVICE_BUSY。所以输出到内存流，
        /// 再把 PCM 写回调用方要的那条流。
        /// </summary>
        public static bool Render(string tokenId, string text, out short[] samples, out Pcm.Format format)
        {
            samples = null;
            format = null;
            ISpVoice voice = null;
            ISpObjectToken token = null;
            ISpStream sink = null;
            try
            {
                voice = (ISpVoice)Activator.CreateInstance(Type.GetTypeFromCLSID(ClsidSpVoice));
                if (voice == null)
                {
                    return false;
                }
                if (!string.IsNullOrEmpty(tokenId))
                {
                    token = FindToken(tokenId);
                    if (token != null)
                    {
                        voice.SetVoice(token);
                    }
                }
                // 用 SAPI 自家的内存流：SAPI 只认自家的流当输出。
                sink = (ISpStream)Activator.CreateInstance(
                    Type.GetTypeFromProgID("SAPI.SpMemoryStream"));
                IntPtr unknown = Marshal.GetIUnknownForObject(sink);
                int hr = voice.SetOutput(unknown, 1);
                Marshal.Release(unknown);
                if (hr != 0)
                {
                    Log.Write("回落时设输出失败：0x" + hr.ToString("X8"));
                    return false;
                }
                uint stream;
                hr = voice.Speak(text, 0, out stream);
                if (hr != 0)
                {
                    Log.Write("回落渲染失败：0x" + hr.ToString("X8"));
                    return false;
                }
                // 流里的格式：SAPI 已经把音色输出转成它了。
                IntPtr formatPtr;
                if (sink.GetFormat(IntPtr.Zero, out formatPtr) == 0 && formatPtr != IntPtr.Zero)
                {
                    var wave = (WAVEFORMATEX)Marshal.PtrToStructure(formatPtr, typeof(WAVEFORMATEX));
                    Marshal.FreeCoTaskMem(formatPtr);
                    format = new Pcm.Format
                    {
                        Channels = wave.nChannels,
                        SampleRate = (int)wave.nSamplesPerSec,
                        BitsPerSample = wave.wBitsPerSample,
                    };
                }
                else
                {
                    format = new Pcm.Format
                    {
                        Channels = 1,
                        SampleRate = 22050,
                        BitsPerSample = 16,
                    };
                }
                long position;
                sink.Seek(0, 0, out position);
                var buffer = new MemoryStream();
                IntPtr chunk = Marshal.AllocHGlobal(32768);
                try
                {
                    uint read;
                    while (sink.Read(chunk, 32768, out read) == 0 && read > 0)
                    {
                        var bytes = new byte[read];
                        Marshal.Copy(chunk, bytes, 0, (int)read);
                        buffer.Write(bytes, 0, bytes.Length);
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(chunk);
                }
                byte[] pcm = buffer.ToArray();
                if (pcm.Length == 0)
                {
                    Log.Write("回落渲染没有拿到音频");
                    return false;
                }
                var decoded = new short[pcm.Length / 2];
                Buffer.BlockCopy(pcm, 0, decoded, 0, decoded.Length * 2);
                samples = decoded;
                return true;
            }
            catch (Exception ex)
            {
                Exception inner = ex.InnerException ?? ex;
                Log.Write("回落渲染异常：" + inner.GetType().Name + " " + inner.Message);
                return false;
            }
            finally
            {
                if (sink != null)
                {
                    Marshal.ReleaseComObject(sink);
                }
                if (token != null)
                {
                    Marshal.ReleaseComObject(token);
                }
                if (voice != null)
                {
                    Marshal.ReleaseComObject(voice);
                }
            }
        }

        /// <summary>用指定音色念一段文字；音色为空时用系统默认。</summary>
        public static bool Speak(string tokenId, string text)
        {
            ISpVoice voice = null;
            try
            {
                voice = (ISpVoice)Activator.CreateInstance(Type.GetTypeFromCLSID(ClsidSpVoice));
                if (voice == null)
                {
                    return false;
                }
                if (!string.IsNullOrEmpty(tokenId))
                {
                    ISpObjectToken token = FindToken(tokenId);
                    if (token != null)
                    {
                        voice.SetVoice(token);
                    }
                }
                uint stream;
                int spoken = voice.Speak(text, 0, out stream);
                if (spoken != 0)
                {
                    Log.Write("系统音色朗读返回：0x" + spoken.ToString("X8"));
                }
                return spoken == 0;
            }
            catch (Exception ex)
            {
                Log.Write("系统音色朗读失败：" + ex.Message);
                return false;
            }
            finally
            {
                if (voice != null)
                {
                    Marshal.ReleaseComObject(voice);
                }
            }
        }

        public static string TokenId(ISpObjectToken token)
        {
            try
            {
                IntPtr text;
                if (token.GetId(out text) != 0 || text == IntPtr.Zero)
                {
                    return null;
                }
                string id = Marshal.PtrToStringUni(text);
                Marshal.FreeCoTaskMem(text);
                return id;
            }
            catch (Exception)
            {
                return null;
            }
        }

        public static string TokenName(ISpObjectToken token)
        {
            try
            {
                IntPtr text;
                if (token.GetStringValue("Name", out text) != 0 || text == IntPtr.Zero)
                {
                    return null;
                }
                string name = Marshal.PtrToStringUni(text);
                Marshal.FreeCoTaskMem(text);
                return name;
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>按令牌 id 找音色。</summary>
        public static ISpObjectToken FindToken(string tokenId)
        {
            ISpObjectTokenCategory category = null;
            IEnumSpObjectTokens enumerator = null;
            try
            {
                category = GetCategory();
                if (category == null)
                {
                    return null;
                }
                if (category.EnumTokens(null, null, out enumerator) != 0 || enumerator == null)
                {
                    return null;
                }
                var tokens = new IntPtr[1];
                uint fetched;
                int hr = enumerator.Next(1, tokens, out fetched);
                while (hr == 0 && fetched > 0)
                {
                    var token = Marshal.GetObjectForIUnknown(tokens[0]) as ISpObjectToken;
                    Marshal.Release(tokens[0]);
                    if (token != null)
                    {
                        string id = TokenId(token);
                        if (!string.IsNullOrEmpty(id)
                            && string.Equals(id, tokenId, StringComparison.OrdinalIgnoreCase))
                        {
                            return token;
                        }
                        Marshal.ReleaseComObject(token);
                    }
                    hr = enumerator.Next(1, tokens, out fetched);
                }
            }
            catch (Exception ex)
            {
                Log.Write("查找音色失败：" + ex.Message);
            }
            finally
            {
                if (enumerator != null) { Marshal.ReleaseComObject(enumerator); }
                if (category != null) { Marshal.ReleaseComObject(category); }
            }
            return null;
        }

        /// <summary>音色类别对象：默认音色与枚举都从这里走。</summary>
        public static ISpObjectTokenCategory GetCategory()
        {
            ISpVoice voice = null;
            ISpObjectToken token = null;
            try
            {
                voice = (ISpVoice)Activator.CreateInstance(Type.GetTypeFromCLSID(ClsidSpVoice));
                if (voice == null)
                {
                    return null;
                }
                if (voice.GetVoice(out token) != 0 || token == null)
                {
                    return null;
                }
                ISpObjectTokenCategory category;
                if (token.GetCategory(out category) != 0)
                {
                    return null;
                }
                return category;
            }
            catch (Exception ex)
            {
                Log.Write("取音色类别失败：" + ex.Message);
                return null;
            }
            finally
            {
                if (token != null) { Marshal.ReleaseComObject(token); }
                if (voice != null) { Marshal.ReleaseComObject(voice); }
            }
        }
    }

    /// <summary>
    /// 托管类工厂：原生桩（sapi_stub.rs）通过 COM 找到它，再由它造出引擎。
    /// SAPI 自己加载引擎 DLL 并找 DllGetClassObject，托管程序集没有这个导出。
    /// </summary>
    [ComVisible(true)]
    [Guid("2B9D7E64-0F3A-4C58-9D1E-7A6C5B8E2F40")]
    [ClassInterface(ClassInterfaceType.None)]
    [ProgId("VoiceHub.Sapi.ClassFactory")]
    public sealed class ClassFactory : IClassFactory
    {
        public int CreateInstance(IntPtr pUnkOuter, ref Guid riid, out IntPtr ppvObject)
        {
            ppvObject = IntPtr.Zero;
            try
            {
                var engine = new TtsEngine();
                IntPtr unknown = Marshal.GetIUnknownForObject(engine);
                IntPtr result;
                int hr = Marshal.QueryInterface(unknown, ref riid, out result);
                Marshal.Release(unknown);
                if (hr != 0)
                {
                    return hr;
                }
                ppvObject = result;
                return 0;
            }
            catch (Exception ex)
            {
                Log.Write("创建引擎失败：" + ex.Message);
                return unchecked((int)0x80004005);
            }
        }

        public int LockServer(int fLockServer)
        {
            return 0;
        }
    }

    /// <summary>SAPI 语音引擎：实现 ISpTTSEngine 与 ISpObjectWithToken。</summary>
    [ComVisible(true)]
    [Guid("6F3C0A5E-1C7B-4D2E-9E4B-6A0D8F35C211")]
    [ClassInterface(ClassInterfaceType.None)]
    [ProgId("VoiceHub.Sapi.TtsEngine")]
    public sealed class TtsEngine : ISpTTSEngine, ISpObjectWithToken, ISpNotifySource
    {
        public const string ClassId = "{6F3C0A5E-1C7B-4D2E-9E4B-6A0D8F35C211}";

        /// <summary>SAPI 给过来的音色令牌。</summary>
        private IntPtr _token = IntPtr.Zero;

        /// <summary>SAPI 的「输出是 WAVEFORMATEX」格式 ID（SAPI 传进来的就是这个）。</summary>
        private static readonly Guid SpDfidWaveFormatEx =
            new Guid("c31adbae-527f-4ff5-a230-f62bb61ff70c");

        /// <summary>回落时防止自我调用造成递归。</summary>
        [ThreadStatic]
        private static bool _inFallback;

        public TtsEngine()
        {
            Log.Write("引擎被创建");
        }

        #region ISpTTSEngine

        public int Speak(uint dwSpeakFlags, ref Guid rguidFormatId, IntPtr pWaveFormatEx,
            IntPtr pTextFragList, ISpTTSEngineSite pOutputSite)
        {
            try
            {
                string text = CollectText(pTextFragList);
                if (text.Length == 0)
                {
                    return 0;
                }
                BridgeConfig config = BridgeConfig.Load();
                WAVEFORMATEX target = pWaveFormatEx == IntPtr.Zero
                    ? new WAVEFORMATEX
                    {
                        wFormatTag = 1,
                        nChannels = 1,
                        nSamplesPerSec = 22050,
                        nAvgBytesPerSec = 44100,
                        nBlockAlign = 2,
                        wBitsPerSample = 16,
                        cbSize = 0,
                    }
                    : (WAVEFORMATEX)Marshal.PtrToStructure(pWaveFormatEx, typeof(WAVEFORMATEX));
                byte[] audio = null;
                try
                {
                    audio = TtsClient.Synthesize(config, text);
                }
                catch (Exception ex)
                {
                    Log.Write("项目接口失败，回落系统音色：" + ex.Message);
                }
                if (audio != null)
                {
                    Pcm.Format format;
                    short[] samples;
                    if (Pcm.TryDecodeWav(audio, out format, out samples))
                    {
                        try
                        {
                            byte[] converted = Pcm.Convert(samples, format, target);
                            Log.Write("项目接口合成完成：" + text.Length + " 字 / "
                                      + format.SampleRate + "Hz / " + converted.Length + " 字节");
                            Write(pOutputSite, converted);
                            return 0;
                        }
                        catch (Exception ex)
                        {
                            Log.Write("音频转换失败，回落系统音色：" + ex.Message);
                        }
                    }
                    else
                    {
                        Log.Write("返回的音频不是 16bit PCM WAV，回落系统音色");
                    }
                }
                Fallback(config, text, pOutputSite, target);
                return 0;
            }
            catch (Exception ex)
            {
                Log.Write("Speak 异常：" + ex);
                return 0;
            }
        }

        public int GetOutputFormat(IntPtr pTargetFmtId, IntPtr pTargetWaveFormatEx,
            IntPtr pOutputFormatId, out IntPtr ppCoMemOutputWaveFormatEx)
        {
            ppCoMemOutputWaveFormatEx = IntPtr.Zero;
            try
            {
                string asked = "null";
                if (pTargetFmtId != IntPtr.Zero)
                {
                    asked = ((Guid)Marshal.PtrToStructure(pTargetFmtId, typeof(Guid))).ToString();
                }
                string askedParams = "null";
                if (pTargetWaveFormatEx != IntPtr.Zero)
                {
                    var probe = (WAVEFORMATEX)Marshal.PtrToStructure(
                        pTargetWaveFormatEx, typeof(WAVEFORMATEX));
                    askedParams = probe.wFormatTag + "/" + probe.nChannels + "ch/"
                        + probe.nSamplesPerSec + "Hz/" + probe.wBitsPerSample + "bit";
                }
                WAVEFORMATEX wanted = new WAVEFORMATEX
                {
                    wFormatTag = 1,
                    nChannels = 1,
                    nSamplesPerSec = 22050,
                    nAvgBytesPerSec = 44100,
                    nBlockAlign = 2,
                    wBitsPerSample = 16,
                    cbSize = 0,
                };
                if (pTargetWaveFormatEx != IntPtr.Zero)
                {
                    var target = (WAVEFORMATEX)Marshal.PtrToStructure(
                        pTargetWaveFormatEx, typeof(WAVEFORMATEX));
                    if (target.wFormatTag == 1 && target.wBitsPerSample == 16
                        && target.nChannels >= 1 && target.nChannels <= 2
                        && target.nSamplesPerSec >= 8000 && target.nSamplesPerSec <= 48000)
                    {
                        wanted = target;
                    }
                }
                IntPtr memory = Marshal.AllocCoTaskMem(Marshal.SizeOf(typeof(WAVEFORMATEX)));
                Marshal.StructureToPtr(wanted, memory, false);
                ppCoMemOutputWaveFormatEx = memory;
                // 格式 ID 一定要回写：SAPI 问「你要什么格式」时可能传空指针，也可能传全零 GUID，
                // 这两种都当成「没有指定」，回自己的默认格式。
                if (pOutputFormatId != IntPtr.Zero)
                {
                    Guid formatId = SpDfidWaveFormatEx;
                    if (pTargetFmtId != IntPtr.Zero)
                    {
                        Guid askedId = (Guid)Marshal.PtrToStructure(pTargetFmtId, typeof(Guid));
                        if (askedId != Guid.Empty)
                        {
                            formatId = askedId;
                        }
                    }
                    Marshal.StructureToPtr(formatId, pOutputFormatId, false);
                }
                Log.Write("GetOutputFormat 回：" + wanted.wFormatTag + "/" + wanted.nChannels + "ch/"
                    + wanted.nSamplesPerSec + "Hz/" + wanted.wBitsPerSample + "bit"
                    + "（问的是 " + asked + " " + askedParams + "）");
                return 0;
            }
            catch (Exception ex)
            {
                Log.Write("GetOutputFormat 异常：" + ex);
                return unchecked((int)0x80004005);
            }
        }

        #endregion

        ~TtsEngine()
        {
            if (_token != IntPtr.Zero)
            {
                Marshal.Release(_token);
                _token = IntPtr.Zero;
            }
        }

        #region ISpObjectWithToken

        public int SetObjectToken(IntPtr pToken)
        {
            // 令牌里没有引擎要用的数据，配置走 bridge.json；令牌按 COM 引用计数持有，SAPI 会来问。
            if (_token != IntPtr.Zero)
            {
                Marshal.Release(_token);
                _token = IntPtr.Zero;
            }
            if (pToken != IntPtr.Zero)
            {
                Marshal.AddRef(pToken);
                _token = pToken;
            }
            Log.Write("SetObjectToken");
            return 0;
        }

        public int GetObjectToken(out IntPtr ppToken)
        {
            if (_token != IntPtr.Zero)
            {
                Marshal.AddRef(_token);
            }
            ppToken = _token;
            return 0;
        }

        #endregion

        #region ISpNotifySource

        public int SetNotifySink(IntPtr pNotifySink) { return 0; }
        public int SetNotifyWindowMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam) { return 0; }
        public int SetNotifyCallbackFunction(IntPtr pfnCallback, IntPtr wParam, IntPtr lParam) { return 0; }
        public int SetNotifyCallbackInterface(IntPtr pSpCallback, IntPtr wParam, IntPtr lParam) { return 0; }
        public int SetNotifyWin32Event() { return 0; }
        public int WaitForNotifyEvent(uint dwMilliseconds) { return 0; }
        public IntPtr GetNotifyEventHandle() { return IntPtr.Zero; }

        #endregion

        private static string CollectText(IntPtr pTextFragList)
        {
            var builder = new StringBuilder();
            IntPtr current = pTextFragList;
            int guard = 0;
            while (current != IntPtr.Zero && guard++ < 4096)
            {
                var fragment = (SPVTEXTFRAG)Marshal.PtrToStructure(current, typeof(SPVTEXTFRAG));
                string piece = fragment.pTextStart == IntPtr.Zero || fragment.ulTextLen == 0
                    ? string.Empty
                    : Marshal.PtrToStringUni(fragment.pTextStart, (int)fragment.ulTextLen);
                switch (fragment.State.eAction)
                {
                    case 0: // SPVA_Speak
                    case 4: // SPVA_SpellOut
                        builder.Append(piece);
                        break;
                    case 1: // SPVA_Silence
                        builder.Append(' ');
                        break;
                    default:
                        break;
                }
                current = fragment.pNext;
            }
            return builder.ToString().Trim();
        }

        private static void Write(ISpTTSEngineSite site, byte[] audio)
        {
            const int Chunk = 16 * 1024;
            IntPtr buffer = Marshal.AllocHGlobal(Chunk);
            try
            {
                int offset = 0;
                while (offset < audio.Length)
                {
                    uint actions = site.GetActions();
                    if ((actions & 0x1) != 0) // SPVES_ABORT
                    {
                        return;
                    }
                    int size = Math.Min(Chunk, audio.Length - offset);
                    Marshal.Copy(audio, offset, buffer, size);
                    uint written;
                    int hr = site.Write(buffer, (uint)size, out written);
                    if (hr != 0 || written == 0)
                    {
                        return;
                    }
                    offset += (int)written;
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        /// <summary>项目不可用时用系统音色接着念。</summary>
        private static void Fallback(BridgeConfig config, string text,
            ISpTTSEngineSite site, WAVEFORMATEX target)
        {
            if (_inFallback)
            {
                return;
            }
            _inFallback = true;
            try
            {
                string tokenId = PickFallbackToken(config);
                short[] samples;
                Pcm.Format format;
                if (!SystemVoices.Render(tokenId, text, out samples, out format))
                {
                    Log.Write("回落失败：系统音色没出声");
                    return;
                }
                byte[] converted = Pcm.Convert(samples, format, target);
                Write(site, converted);
                Log.Write("已回落系统音色：" + tokenId + " / " + converted.Length + " 字节");
            }
            catch (Exception ex)
            {
                Log.Write("回落失败：" + ex.Message);
            }
            finally
            {
                _inFallback = false;
            }
        }

        /// <summary>挑一个不是自己的音色，避免回落时又调回自己。</summary>
        private static string PickFallbackToken(BridgeConfig config)
        {
            Dictionary<string, string> voices = SystemVoices.List();
            if (voices.Count == 0)
            {
                return null;
            }
            foreach (var pair in voices)
            {
                if (!string.IsNullOrEmpty(config.FallbackVoice)
                    && string.Equals(pair.Value, config.FallbackVoice, StringComparison.OrdinalIgnoreCase))
                {
                    return pair.Key;
                }
            }
            foreach (var pair in voices)
            {
                if (!string.Equals(pair.Value, config.VoiceName, StringComparison.OrdinalIgnoreCase))
                {
                    return pair.Key;
                }
            }
            return null;
        }
    }
}
