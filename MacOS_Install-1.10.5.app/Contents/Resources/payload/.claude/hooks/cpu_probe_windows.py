"""One Windows system snapshot instead of a system-wide query for every thread.

SYSTEM_PROCESS_INFORMATION / SYSTEM_THREAD_INFORMATION, native pointer layout.
NtQuerySystemInformation is dynamically loaded and every offset is checked.
"""
import ctypes as C


class UnicodeString(C.Structure):
    _fields_ = [('length', C.c_ushort), ('maximum', C.c_ushort), ('buffer', C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [('next', C.c_uint32), ('thread_count', C.c_uint32),
                ('private_working_set', C.c_int64), ('hard_faults', C.c_uint32),
                ('threads_high', C.c_uint32), ('cycles', C.c_uint64),
                ('created', C.c_int64), ('user', C.c_int64), ('kernel', C.c_int64),
                ('name', UnicodeString), ('priority', C.c_int32),
                ('pid', C.c_void_p), ('parent', C.c_void_p),
                ('handles', C.c_uint32), ('session', C.c_uint32), ('key', C.c_void_p),
                ('peak_virtual', C.c_size_t), ('virtual', C.c_size_t), ('faults', C.c_uint32),
                ('memory', C.c_size_t * 9), ('io', C.c_int64 * 6)]


class ThreadInfo(C.Structure):
    _fields_ = [('kernel', C.c_int64), ('user', C.c_int64), ('created', C.c_int64),
                ('wait', C.c_uint32), ('start', C.c_void_p),
                ('pid', C.c_void_p), ('tid', C.c_void_p),
                ('priority', C.c_int32), ('base_priority', C.c_int32),
                ('switches', C.c_uint32), ('state', C.c_uint32), ('reason', C.c_uint32)]


def parse(buffer, length):
    base, offset = C.addressof(buffer), 0
    rows = []
    while True:
        if offset + C.sizeof(ProcessInfo) > length:
            raise ValueError('Truncated Windows process snapshot')
        proc = ProcessInfo.from_buffer(buffer, offset)
        limit = offset + proc.next if proc.next else length
        if limit > length or limit < offset + C.sizeof(ProcessInfo):
            raise ValueError('Invalid Windows process offset')
        if proc.name.length:
            ptr = proc.name.buffer or 0
            if proc.name.length % 2 or not (base <= ptr <= base + length - proc.name.length):
                raise ValueError('Invalid Windows process name')
            name = C.string_at(ptr, proc.name.length).decode('utf-16-le')
        else:
            name = ''
        if name.lower() in ('code.exe', 'code-insiders.exe'):
            start = offset + C.sizeof(ProcessInfo)
            if start + proc.thread_count * C.sizeof(ThreadInfo) > limit:
                raise ValueError('Truncated Windows thread snapshot')
            threads = {}
            for i in range(proc.thread_count):
                thread = ThreadInfo.from_buffer(buffer, start + i * C.sizeof(ThreadInfo))
                if thread.pid != proc.pid:
                    raise ValueError('Windows thread/process identity mismatch')
                threads[(thread.tid, thread.created)] = (thread.user + thread.kernel) / 10000000
            rows.append(dict(pid=proc.pid, created=proc.created,
                             total=(proc.user + proc.kernel) / 10000000, threads=threads))
        if not proc.next:
            return rows
        offset = limit


class WindowsSnapshot:
    def __init__(self):
        self.query = C.WinDLL('ntdll').NtQuerySystemInformation
        self.query.argtypes = [C.c_uint32, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)]
        self.query.restype = C.c_int32
        self.size = 1024 * 1024

    def read(self):
        for _ in range(5):
            buffer = C.create_string_buffer(self.size)
            needed = C.c_uint32()
            status = self.query(5, buffer, self.size, C.byref(needed))
            if status == 0:
                return parse(buffer, needed.value)
            if status & 0xffffffff != 0xc0000004:
                raise OSError('Windows process snapshot failed: %08x' % (status & 0xffffffff))
            self.size = max(self.size * 2, needed.value + 65536)
            if self.size > 64 * 1024 * 1024:
                break
        raise OSError('Windows process snapshot exceeded size/retry limit')
