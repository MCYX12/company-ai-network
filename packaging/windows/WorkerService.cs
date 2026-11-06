// SCM lifetime wrapper; the worker and all descendants belong to a kill-on-close job.
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Threading;

public sealed class WorkerService : ServiceBase {
    readonly string python, root;
    IntPtr job, process;
    Thread observer;
    volatile bool stopping;
    [StructLayout(LayoutKind.Sequential)] struct STARTUPINFO {
        public int cb; public IntPtr reserved, desktop, title;
        public int x,y,xSize,ySize,xCount,yCount,fill,flags;
        public short show,reserved2; public IntPtr reservedPtr,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct PROCESSINFO { public IntPtr process,thread; public int pid,tid; }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcess(string app,string command,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref STARTUPINFO si,out PROCESSINFO pi);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attr,string name);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int type,IntPtr data,uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint ms);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr handle,uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static string Quote(string s) { if(s.IndexOf('"')>=0||s.IndexOf('\n')>=0||s.IndexOf('\r')>=0)throw new ArgumentException("Invalid path");return "\""+s+"\""; }
    WorkerService(string name,string python,string root) {
        ServiceName=name;this.python=Path.GetFullPath(python);this.root=Path.GetFullPath(root);
        CanStop=true;CanShutdown=true;AutoLog=false;
    }
    protected override void OnStart(string[] args) {
        if(!File.Exists(python)||!File.Exists(Path.Combine(root,"run-worker.py")))throw new InvalidOperationException("Missing worker installation");
        stopping=false;job=CreateJobObject(IntPtr.Zero,null);
        // JOBOBJECT_EXTENDED_LIMIT_INFORMATION: BasicLimitInformation.LimitFlags offset16;
        // native x64 size144 (x86 size112), JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000.
        int size=IntPtr.Size==8?144:112;IntPtr data=Marshal.AllocHGlobal(size);
        try { for(int i=0;i<size;i++)Marshal.WriteByte(data,i,0);Marshal.WriteInt32(data,16,0x2000);
            if(job==IntPtr.Zero||!SetInformationJobObject(job,9,data,(uint)size))throw new InvalidOperationException("Job creation failed");
        } finally { Marshal.FreeHGlobal(data); }
        STARTUPINFO si=new STARTUPINFO();si.cb=Marshal.SizeOf(si);PROCESSINFO pi;
        if(!CreateProcess(python,Quote(python)+" -u "+Quote(Path.Combine(root,"run-worker.py")),IntPtr.Zero,IntPtr.Zero,false,0x08000004,IntPtr.Zero,root,ref si,out pi)) {
            CloseHandle(job);job=IntPtr.Zero;throw new InvalidOperationException("Worker start failed: "+Marshal.GetLastWin32Error());
        }
        process=pi.process;
        try { if(!AssignProcessToJobObject(job,process)||ResumeThread(pi.thread)==0xffffffff)throw new InvalidOperationException("Worker isolation failed"); }
        catch { TerminateProcess(process,1);CloseHandle(process);CloseHandle(job);process=job=IntPtr.Zero;throw; }
        finally { CloseHandle(pi.thread); }
        observer=new Thread(()=>{WaitForSingleObject(process,0xffffffff);if(!stopping){uint code;GetExitCodeProcess(process,out code);ExitCode=code==0?1:(int)code;Stop();}});
        observer.IsBackground=true;observer.Start();
    }
    protected override void OnStop() {
        stopping=true;
        if(job!=IntPtr.Zero){CloseHandle(job);job=IntPtr.Zero;}
        if(process!=IntPtr.Zero){WaitForSingleObject(process,10000);if(observer!=null&&Thread.CurrentThread!=observer)observer.Join(11000);CloseHandle(process);process=IntPtr.Zero;}
    }
    protected override void OnShutdown(){OnStop();}
    public static void Main(string[] args) {
        if(args.Length!=3)throw new ArgumentException("service-name python-path installation-root required");
        ServiceBase.Run(new WorkerService(args[0],args[1],args[2]));
    }
}
