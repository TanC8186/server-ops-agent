- Home
- Products
- Red Hat Enterprise Linux
- 9
- Monitoring and managing system status and performance
- Chapter 1. Overview of performance monitoring options

# Chapter 1. Overview of performance monitoring options

You can use following tools for monitoring and optimizing performance of your Red Hat Enterprise Linux:

- Performance Co-Pilot (pcp) is a suite of tools for monitoring, visualizing, storing, and analyzing system-level performance measurements. It allows the monitoring and management of real-time data, as well as logging and retrieval of historical data.
-

RHEL provides several command-line tools to monitor a system outside runlevel 5. The following are the built-in command-line tools:

-

`The procps-ng package provides following utilities:

- `The top utility gives a dynamic view of processes in a running system. It displays a variety of information, including a system summary and a list of tasks currently being managed by the Linux kernel.
- `The ps utility captures a snapshot of the selected group of active processes. By default, the examined group is limited to processes that are owned by the current user and associated with the terminal where the ps command is executed.
- `The virtual memory statistics (vmstat) utility provides instant reports of processes, memory, paging, block input/output, interrupts, and CPU activity in the system.

- `The sysstat package provides the system activity reporter (sar) utility, which collects and reports information about system activity for the current day.

- `perf uses hardware performance counters and kernel trace-points to track the impact of other commands and applications on a system.
- `bcc-tools, a set of performance analysis utilities, built on top of Berkeley Packet Filter (BPF) Compiler Collection (BCC). It provides over 100 Extended BPF (eBPF) scripts that monitor kernel activities. For more information about each of this tool, see the man page describing how to use it and what functions it performs.
- `The kernel-tools package provides the turbostat utility that reports on processor topology, frequency, idle power-state statistics, temperature, and power usage on the Intel 64 processors.
- `The sysstat package provides the iostat utility that monitors and reports on loading of system I/O device to help administrators make decisions about how to balance I/O load between physical disks.
- `ss prints statistical information about sockets, allowing administrators to assess socket performance over time. You can use ss over netstat.
- `The irqbalance utility distributes hardware interrupts across processors to improve system performance.
- `The numactl package provides the numastat utility. By default, numastat displays per-node Non-Uniform Memory Access (NUMA) hit and miss system statistics from the kernel memory allocator. High numa_hit values and low numa_miss values indicate optimal performance.
- `numad is an automatic NUMA affinity management daemon. It monitors NUMA topology and resource usage within a system that dynamically improves NUMA resource allocation, management, and therefore system performance.
- `SystemTap is a programmable tracing/probing/debugging system for the kernel and userspace, and includes many brief scripts.
- `valgrind runs uninstrumented userspace programs under supervision, to find memory errors, allocation statistics, concurrency violations.
- `The intel-cmt-cat package provides the pqos utility to monitor and control CPU cache and memory bandwidth on recent Intel processors.
