/*
 * Project Sentinel — Hardware Telemetry Engine
 * 
 * A low-overhead, high-frequency hardware monitor that bridges raw device
 * metrics into a structured redo-log consumed by the AI reasoning layer.
 *
 * Cross-platform: Linux (sysfs/proc) + Windows (WinAPI)
 * Build: g++ -O2 -std=c++17 -pthread -o sentinel_engine sentinel_engine.cpp
 *
 * Author: Sentinel Engineering Team
 * License: Apache 2.0
 */

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <chrono>
#include <thread>
#include <atomic>
#include <ctime>
#include <iomanip>
#include <vector>
#include <numeric>
#include <cstring>
#include <csignal>
#include <filesystem>
#include <mutex>

#ifdef _WIN32
  #include <windows.h>
  #include <psapi.h>
  #include <powerbase.h>
  #pragma comment(lib, "PowrProf.lib")
#else
  #include <sys/sysinfo.h>
  #include <sys/statvfs.h>
  #include <unistd.h>
  #include <fstream>
#endif

namespace fs = std::filesystem;

// ─── Constants ───────────────────────────────────────────────────────────────

static constexpr int    POLL_INTERVAL_MS      = 500;
static constexpr int    LSN_EPOCH_BASE        = 1000000;
static constexpr double RAM_CRITICAL_PCT      = 90.0;
static constexpr double RAM_WARN_PCT          = 75.0;
static constexpr double THERMAL_CRITICAL_C    = 85.0;
static constexpr double THERMAL_WARN_C        = 70.0;
static constexpr int    ROLLING_WINDOW        = 10;   // samples for trend analysis
static const std::string LOG_PATH             = "../logs/sentinel_redo.log";
static const std::string CONFIG_PATH          = "../configs/engine.conf";

// ─── Global State ─────────────────────────────────────────────────────────────

std::atomic<bool>  g_running{true};
std::atomic<long>  g_lsn{LSN_EPOCH_BASE};
std::mutex         g_log_mutex;

// ─── Structs ─────────────────────────────────────────────────────────────────

struct HardwareSnapshot {
    long        lsn;
    double      ram_used_pct;
    double      ram_free_mb;
    double      ram_total_mb;
    double      cpu_load_pct;
    double      thermal_c;          // CPU temp estimate
    double      battery_pct;        // -1 if no battery / desktop
    bool        battery_charging;
    double      disk_used_pct;
    long long   timestamp_ms;
    std::string pressure_state;     // NOMINAL | WARN | CRITICAL
    std::string trend;              // RISING | STABLE | FALLING
    double      ram_delta_pct;      // change vs previous sample
};

struct RollingStats {
    std::vector<double> ram_history;
    std::vector<double> cpu_history;

    void push(double ram, double cpu) {
        if ((int)ram_history.size() >= ROLLING_WINDOW) {
            ram_history.erase(ram_history.begin());
            cpu_history.erase(cpu_history.begin());
        }
        ram_history.push_back(ram);
        cpu_history.push_back(cpu);
    }

    double ram_trend() const {
        if (ram_history.size() < 3) return 0.0;
        // simple linear slope over last N samples
        int n = ram_history.size();
        double sum_x = 0, sum_y = 0, sum_xy = 0, sum_x2 = 0;
        for (int i = 0; i < n; ++i) {
            sum_x  += i;
            sum_y  += ram_history[i];
            sum_xy += i * ram_history[i];
            sum_x2 += i * i;
        }
        double denom = n * sum_x2 - sum_x * sum_x;
        if (std::abs(denom) < 1e-9) return 0.0;
        return (n * sum_xy - sum_x * sum_y) / denom;
    }
};

// ─── Platform Metric Readers ──────────────────────────────────────────────────

#ifdef _WIN32

double read_ram_pct(double &free_mb, double &total_mb) {
    MEMORYSTATUSEX ms;
    ms.dwLength = sizeof(ms);
    GlobalMemoryStatusEx(&ms);
    total_mb = ms.ullTotalPhys  / (1024.0 * 1024.0);
    free_mb  = ms.ullAvailPhys  / (1024.0 * 1024.0);
    return ms.dwMemoryLoad;  // 0–100
}

double read_cpu_pct() {
    static FILETIME prev_idle{}, prev_kernel{}, prev_user{};
    FILETIME idle, kernel, user, dummy;
    GetSystemTimes(&idle, &kernel, &user);

    auto sub = [](FILETIME a, FILETIME b) -> unsigned long long {
        ULARGE_INTEGER ua, ub;
        ua.LowPart = a.dwLowDateTime; ua.HighPart = a.dwHighDateTime;
        ub.LowPart = b.dwLowDateTime; ub.HighPart = b.dwHighDateTime;
        return ua.QuadPart - ub.QuadPart;
    };

    unsigned long long d_idle   = sub(idle,   prev_idle);
    unsigned long long d_kernel = sub(kernel, prev_kernel);
    unsigned long long d_user   = sub(user,   prev_user);

    prev_idle = idle; prev_kernel = kernel; prev_user = user;
    unsigned long long total = d_kernel + d_user;
    if (total == 0) return 0.0;
    return 100.0 * (1.0 - (double)d_idle / total);
}

double read_battery_pct(bool &charging) {
    SYSTEM_POWER_STATUS sps;
    GetSystemPowerStatus(&sps);
    charging = (sps.ACLineStatus == 1);
    if (sps.BatteryLifePercent == 255) return -1.0;
    return sps.BatteryLifePercent;
}

double read_thermal_c() {
    // Windows: thermal via WMI is heavy — estimate from CPU load as proxy
    // Real deployments should use OpenHardwareMonitor COM interface
    double cpu = read_cpu_pct();
    return 35.0 + cpu * 0.5;  // heuristic: 35°C idle, 85°C at 100% load
}

double read_disk_pct() {
    ULARGE_INTEGER free_bytes, total_bytes, dummy;
    GetDiskFreeSpaceExA("C:\\", &free_bytes, &total_bytes, &dummy);
    if (total_bytes.QuadPart == 0) return 0.0;
    return 100.0 * (1.0 - (double)free_bytes.QuadPart / total_bytes.QuadPart);
}

#else  // Linux

double read_ram_pct(double &free_mb, double &total_mb) {
    struct sysinfo si;
    sysinfo(&si);
    total_mb = si.totalram  * si.mem_unit / (1024.0 * 1024.0);
    free_mb  = si.freeram   * si.mem_unit / (1024.0 * 1024.0);
    // include buffers/cache as available (Linux semantics)
    double avail_mb = (si.freeram + si.bufferram) * si.mem_unit / (1024.0 * 1024.0);

    // /proc/meminfo has a more accurate MemAvailable
    std::ifstream mi("/proc/meminfo");
    std::string line;
    while (std::getline(mi, line)) {
        if (line.rfind("MemAvailable:", 0) == 0) {
            long long kb;
            sscanf(line.c_str() + 13, "%lld", &kb);
            avail_mb = kb / 1024.0;
            break;
        }
    }
    free_mb = avail_mb;
    return 100.0 * (1.0 - avail_mb / total_mb);
}

double read_cpu_pct() {
    static long long prev_idle = 0, prev_total = 0;
    std::ifstream f("/proc/stat");
    std::string cpu;
    long long user, nice, sys, idle, iowait, irq, softirq;
    f >> cpu >> user >> nice >> sys >> idle >> iowait >> irq >> softirq;
    long long total = user + nice + sys + idle + iowait + irq + softirq;
    long long d_idle  = idle  - prev_idle;
    long long d_total = total - prev_total;
    prev_idle = idle; prev_total = total;
    if (d_total == 0) return 0.0;
    return 100.0 * (1.0 - (double)d_idle / d_total);
}

double read_battery_pct(bool &charging) {
    // Try standard Linux power_supply sysfs
    std::string base = "/sys/class/power_supply/";
    for (auto& entry : {"BAT0", "BAT1", "battery"}) {
        std::string path = base + entry + "/capacity";
        std::ifstream f(path);
        if (f.good()) {
            int pct;
            f >> pct;
            std::ifstream sf(base + std::string(entry) + "/status");
            std::string status;
            sf >> status;
            charging = (status == "Charging" || status == "Full");
            return pct;
        }
    }
    charging = true;
    return -1.0;  // no battery (desktop/container)
}

double read_thermal_c() {
    // Try hwmon first (most accurate on Linux)
    std::string hwmon_base = "/sys/class/hwmon/";
    for (int i = 0; i <= 5; ++i) {
        std::string path = hwmon_base + "hwmon" + std::to_string(i) + "/temp1_input";
        std::ifstream f(path);
        if (f.good()) {
            int milli;
            f >> milli;
            return milli / 1000.0;
        }
    }
    // Thermal zone fallback
    std::ifstream f("/sys/class/thermal/thermal_zone0/temp");
    if (f.good()) {
        int milli;
        f >> milli;
        return milli / 1000.0;
    }
    // CPU load heuristic for containerised environments
    // (which is what we're running in)
    double cpu = read_cpu_pct();
    double temp = 38.0 + cpu * 0.47;
    return temp;
}

double read_disk_pct() {
    struct statvfs sv;
    statvfs("/", &sv);
    if (sv.f_blocks == 0) return 0.0;
    return 100.0 * (1.0 - (double)sv.f_bavail / sv.f_blocks);
}

#endif  // platform

// ─── Snapshot Builder ─────────────────────────────────────────────────────────

HardwareSnapshot build_snapshot(RollingStats &stats) {
    HardwareSnapshot s;
    s.lsn = ++g_lsn;

    auto now = std::chrono::system_clock::now();
    s.timestamp_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count();

    s.ram_used_pct    = read_ram_pct(s.ram_free_mb, s.ram_total_mb);
    s.cpu_load_pct    = read_cpu_pct();
    s.thermal_c       = read_thermal_c();
    s.battery_pct     = read_battery_pct(s.battery_charging);
    s.disk_used_pct   = read_disk_pct();

    // Trend analysis
    double prev_ram = stats.ram_history.empty() ? s.ram_used_pct : stats.ram_history.back();
    s.ram_delta_pct = s.ram_used_pct - prev_ram;
    stats.push(s.ram_used_pct, s.cpu_load_pct);
    double slope = stats.ram_trend();

    if      (slope >  0.5)  s.trend = "RISING";
    else if (slope < -0.5)  s.trend = "FALLING";
    else                    s.trend = "STABLE";

    // Composite pressure state (the key innovation: multi-signal fusion)
    bool   bat_low     = (s.battery_pct >= 0 && s.battery_pct < 15 && !s.battery_charging);
    bool   thermal_crit = s.thermal_c >= THERMAL_CRITICAL_C;
    bool   thermal_warn = s.thermal_c >= THERMAL_WARN_C;
    bool   ram_crit     = s.ram_used_pct >= RAM_CRITICAL_PCT;
    bool   ram_warn     = s.ram_used_pct >= RAM_WARN_PCT;

    if (ram_crit || thermal_crit || bat_low)
        s.pressure_state = "CRITICAL";
    else if (ram_warn || thermal_warn)
        s.pressure_state = "WARN";
    else
        s.pressure_state = "NOMINAL";

    return s;
}

// ─── Log Writer ───────────────────────────────────────────────────────────────

std::string format_log_entry(const HardwareSnapshot &s) {
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(1);
    oss << "LSN:" << s.lsn
        << " | TS:" << s.timestamp_ms
        << " | RAM_USED:" << s.ram_used_pct << "%"
        << " | RAM_FREE_MB:" << s.ram_free_mb
        << " | CPU:" << s.cpu_load_pct << "%"
        << " | THERMAL_C:" << s.thermal_c
        << " | BAT:" << s.battery_pct << (s.battery_charging ? "(CHG)" : "(DC)")
        << " | DISK:" << s.disk_used_pct << "%"
        << " | STATE:" << s.pressure_state
        << " | TREND:" << s.trend
        << " | DELTA:" << (s.ram_delta_pct >= 0 ? "+" : "") << s.ram_delta_pct << "%"
        << "\n";
    return oss.str();
}

void write_log(const std::string &entry) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    std::ofstream f(LOG_PATH, std::ios::app);
    if (f.is_open()) {
        f << entry;
        f.flush();
    }
}

// ─── Signal Handler ───────────────────────────────────────────────────────────

void handle_signal(int) {
    g_running = false;
}

// ─── Main Loop ────────────────────────────────────────────────────────────────

int main(int argc, char *argv[]) {
    std::signal(SIGINT,  handle_signal);
    std::signal(SIGTERM, handle_signal);

    // Ensure log directory exists
    fs::create_directories(fs::path(LOG_PATH).parent_path());

    // Print startup banner
    std::cout << "╔══════════════════════════════════════╗\n";
    std::cout << "║      Project Sentinel — Engine       ║\n";
    std::cout << "║  Hardware Telemetry v1.0.0           ║\n";
    std::cout << "╚══════════════════════════════════════╝\n";
    std::cout << "  Log  : " << LOG_PATH << "\n";
    std::cout << "  Poll : " << POLL_INTERVAL_MS << "ms\n";
    std::cout << "  Press Ctrl+C to stop.\n\n";

    RollingStats stats;
    int print_every = 4;  // print to stdout every 4 samples (2s)
    int counter = 0;

    // Warm-up: take two silent reads so CPU delta stabilises
    read_cpu_pct();
    std::this_thread::sleep_for(std::chrono::milliseconds(POLL_INTERVAL_MS));

    while (g_running) {
        HardwareSnapshot snap = build_snapshot(stats);
        std::string entry = format_log_entry(snap);

        write_log(entry);

        if (++counter % print_every == 0) {
            // Colour-coded console output
            std::string colour = "\033[32m";  // green = nominal
            if      (snap.pressure_state == "CRITICAL") colour = "\033[31m";
            else if (snap.pressure_state == "WARN")     colour = "\033[33m";

            std::cout << colour
                      << "[LSN " << snap.lsn << "] "
                      << "RAM:" << std::fixed << std::setprecision(1) << snap.ram_used_pct << "% "
                      << "CPU:" << snap.cpu_load_pct << "% "
                      << "TEMP:" << snap.thermal_c << "°C "
                      << "[" << snap.pressure_state << "/" << snap.trend << "]"
                      << "\033[0m\n";
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(POLL_INTERVAL_MS));
    }

    std::cout << "\nSentinel engine stopped. Total records: "
              << (g_lsn - LSN_EPOCH_BASE) << "\n";
    return 0;
}
