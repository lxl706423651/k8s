/*
 * Native fixed-arrival CPU workload for the E1-A marginal co-location study.
 *
 * Inputs are command-line workload/timing parameters and either an absolute
 * realtime epoch or a shared hostPath epoch file.  The program emits one JSON
 * record to stdout.  It creates no child process and bounds both runtime and
 * backlog memory.  It is intended to run as PID 1 in one Kubernetes Pod.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopRequested = 0;
static volatile uint64_t workSink = 0;

typedef struct {
    double jobsPerSecond;
    uint64_t workUnits;
    uint64_t phaseSeed;
    double maxBacklogSeconds;
    double warmupSeconds;
    double durationSeconds;
    double deadlineMicroseconds;
    int64_t epochUnixNs;
    const char *epochFile;
    const char *podId;
    double calibrateTargetMicroseconds;
    int calibrateSamples;
} Options;

typedef struct {
    uint64_t planned;
    uint64_t started;
    uint64_t completed;
    uint64_t completedOnTime;
    uint64_t dropped;
    uint64_t pending;
    uint64_t maxQueueDepth;
    double maxQueueAgeMs;
    double cpuTimeMs;
    double *completionLatenciesMs;
    double *startLatenciesMs;
    size_t latencyCount;
    size_t latencyCapacity;
} Result;

static void handleSignal(int signalNumber) {
    (void)signalNumber;
    stopRequested = 1;
}

static int64_t readClockNs(clockid_t clockId) {
    struct timespec ts;
    if (clock_gettime(clockId, &ts) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void sleepUntilMonotonic(int64_t targetNs) {
    struct timespec ts = {
        .tv_sec = targetNs / 1000000000LL,
        .tv_nsec = targetNs % 1000000000LL,
    };
    while (!stopRequested && clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) {
    }
}

static uint64_t hashText(const char *text, uint64_t seed) {
    uint64_t value = 1469598103934665603ULL ^ seed;
    for (const unsigned char *cursor = (const unsigned char *)text; *cursor; cursor++) {
        value ^= *cursor;
        value *= 1099511628211ULL;
    }
    value ^= value >> 33;
    value *= 0xff51afd7ed558ccdULL;
    value ^= value >> 33;
    return value;
}

static uint64_t executeWork(uint64_t units, uint64_t state) {
    for (uint64_t index = 0; index < units; index++) {
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        state *= 2685821657736338717ULL;
        state += index + 0x9e3779b97f4a7c15ULL;
    }
    workSink ^= state;
    return state;
}

static double parseDouble(const char *name, const char *value) {
    char *end = NULL;
    errno = 0;
    double parsed = strtod(value, &end);
    if (errno || !end || *end != '\0' || !isfinite(parsed)) {
        fprintf(stderr, "invalid %s: %s\n", name, value);
        exit(2);
    }
    return parsed;
}

static uint64_t parseUint64(const char *name, const char *value) {
    char *end = NULL;
    errno = 0;
    unsigned long long parsed = strtoull(value, &end, 10);
    if (errno || !end || *end != '\0') {
        fprintf(stderr, "invalid %s: %s\n", name, value);
        exit(2);
    }
    return (uint64_t)parsed;
}

static Options parseOptions(int argc, char **argv) {
    Options options = {
        .jobsPerSecond = 0.0,
        .workUnits = 0,
        .phaseSeed = 20260918,
        .maxBacklogSeconds = 5.0,
        .warmupSeconds = 0.0,
        .durationSeconds = 10.0,
        .deadlineMicroseconds = 100000.0,
        .epochUnixNs = 0,
        .epochFile = NULL,
        .podId = NULL,
        .calibrateTargetMicroseconds = 0.0,
        .calibrateSamples = 25,
    };
    for (int index = 1; index < argc; index++) {
        if (index + 1 >= argc) {
            fprintf(stderr, "missing value after %s\n", argv[index]);
            exit(2);
        }
        const char *name = argv[index++];
        const char *value = argv[index];
        if (strcmp(name, "--jobs-per-second") == 0) options.jobsPerSecond = parseDouble(name, value);
        else if (strcmp(name, "--work-units") == 0) options.workUnits = parseUint64(name, value);
        else if (strcmp(name, "--phase-seed") == 0) options.phaseSeed = parseUint64(name, value);
        else if (strcmp(name, "--max-backlog-seconds") == 0) options.maxBacklogSeconds = parseDouble(name, value);
        else if (strcmp(name, "--warmup-seconds") == 0) options.warmupSeconds = parseDouble(name, value);
        else if (strcmp(name, "--duration-seconds") == 0) options.durationSeconds = parseDouble(name, value);
        else if (strcmp(name, "--deadline-microseconds") == 0) options.deadlineMicroseconds = parseDouble(name, value);
        else if (strcmp(name, "--epoch-unix-ns") == 0) options.epochUnixNs = (int64_t)parseUint64(name, value);
        else if (strcmp(name, "--epoch-file") == 0) options.epochFile = value;
        else if (strcmp(name, "--pod-id") == 0) options.podId = value;
        else if (strcmp(name, "--calibrate-target-microseconds") == 0) options.calibrateTargetMicroseconds = parseDouble(name, value);
        else if (strcmp(name, "--calibrate-samples") == 0) options.calibrateSamples = (int)parseUint64(name, value);
        else {
            fprintf(stderr, "unknown option: %s\n", name);
            exit(2);
        }
    }
    if (options.jobsPerSecond < 0.0 || options.maxBacklogSeconds <= 0.0 ||
        options.warmupSeconds < 0.0 || options.durationSeconds <= 0.0 ||
        options.deadlineMicroseconds <= 0.0 || options.calibrateSamples <= 0) {
        fprintf(stderr, "workload values are outside their valid range\n");
        exit(2);
    }
    return options;
}

static int compareDoubles(const void *left, const void *right) {
    double a = *(const double *)left;
    double b = *(const double *)right;
    return (a > b) - (a < b);
}

static double percentile(const double *values, size_t count, double q) {
    if (!count) return NAN;
    double *copy = malloc(count * sizeof(double));
    if (!copy) return NAN;
    memcpy(copy, values, count * sizeof(double));
    qsort(copy, count, sizeof(double), compareDoubles);
    double position = q * (double)(count - 1);
    size_t lower = (size_t)floor(position);
    size_t upper = (size_t)ceil(position);
    double fraction = position - (double)lower;
    double output = copy[lower] * (1.0 - fraction) + copy[upper] * fraction;
    free(copy);
    return output;
}

static void appendLatency(Result *result, double startMs, double completionMs) {
    if (result->latencyCount == result->latencyCapacity) {
        size_t nextCapacity = result->latencyCapacity ? result->latencyCapacity * 2 : 1024;
        double *nextCompletion = realloc(result->completionLatenciesMs, nextCapacity * sizeof(double));
        if (!nextCompletion) {
            perror("realloc");
            exit(2);
        }
        result->completionLatenciesMs = nextCompletion;
        double *nextStart = realloc(result->startLatenciesMs, nextCapacity * sizeof(double));
        if (!nextStart) {
            perror("realloc");
            exit(2);
        }
        result->startLatenciesMs = nextStart;
        result->latencyCapacity = nextCapacity;
    }
    result->startLatenciesMs[result->latencyCount] = startMs;
    result->completionLatenciesMs[result->latencyCount] = completionMs;
    result->latencyCount++;
}

static void calibrateWork(const Options *options) {
    uint64_t units = 100000;
    uint64_t state = 0x123456789abcdefULL;
    for (int round = 0; round < 10; round++) {
        int64_t before = readClockNs(CLOCK_THREAD_CPUTIME_ID);
        state = executeWork(units, state);
        int64_t elapsed = readClockNs(CLOCK_THREAD_CPUTIME_ID) - before;
        if (elapsed <= 0) continue;
        double ratio = (options->calibrateTargetMicroseconds * 1000.0) / (double)elapsed;
        if (ratio > 0.90 && ratio < 1.10) break;
        if (ratio < 0.05) ratio = 0.05;
        if (ratio > 20.0) ratio = 20.0;
        units = (uint64_t)fmax(1.0, floor((double)units * ratio));
    }
    double *samples = calloc((size_t)options->calibrateSamples, sizeof(double));
    for (int sample = 0; sample < options->calibrateSamples; sample++) {
        int64_t before = readClockNs(CLOCK_THREAD_CPUTIME_ID);
        state = executeWork(units, state);
        samples[sample] = (readClockNs(CLOCK_THREAD_CPUTIME_ID) - before) / 1000.0;
    }
    qsort(samples, (size_t)options->calibrateSamples, sizeof(double), compareDoubles);
    double median = samples[options->calibrateSamples / 2];
    uint64_t recommended = (uint64_t)fmax(1.0, floor((double)units * options->calibrateTargetMicroseconds / median));
    printf("{\"mode\":\"calibration\",\"target_cpu_us\":%.3f,\"sample_units\":%" PRIu64
           ",\"median_cpu_us\":%.3f,\"recommended_work_units\":%" PRIu64 ",\"samples\":%d}\n",
           options->calibrateTargetMicroseconds, units, median, recommended, options->calibrateSamples);
    free(samples);
}

static int64_t waitForEpoch(const Options *options) {
    if (options->epochUnixNs > 0) return options->epochUnixNs;
    if (!options->epochFile) {
        fprintf(stderr, "normal mode requires --epoch-unix-ns or --epoch-file\n");
        exit(2);
    }
    while (!stopRequested) {
        FILE *handle = fopen(options->epochFile, "r");
        if (handle) {
            long long value = 0;
            int matched = fscanf(handle, "%lld", &value);
            fclose(handle);
            if (matched == 1 && value > 0) return (int64_t)value;
        }
        struct timespec pause = {.tv_sec = 0, .tv_nsec = 100000000};
        nanosleep(&pause, NULL);
    }
    return 0;
}

static void executePeriod(const Options *options, int64_t startNs, double seconds, int record, Result *result) {
    if (seconds <= 0.0) return;
    int64_t endNs = startNs + (int64_t)llround(seconds * 1e9);
    int64_t cpuBefore = readClockNs(CLOCK_THREAD_CPUTIME_ID);
    if (options->jobsPerSecond == 0.0) {
        sleepUntilMonotonic(endNs);
        if (record) result->cpuTimeMs += (readClockNs(CLOCK_THREAD_CPUTIME_ID) - cpuBefore) / 1e6;
        return;
    }
    int64_t periodNs = (int64_t)llround(1e9 / options->jobsPerSecond);
    uint64_t phaseHash = hashText(options->podId, options->phaseSeed);
    int64_t phaseNs = (int64_t)(phaseHash % (uint64_t)periodNs);
    int64_t nextArrivalNs = startNs + phaseNs;
    size_t queueCapacity = (size_t)ceil(options->maxBacklogSeconds * options->jobsPerSecond);
    if (queueCapacity < 1) queueCapacity = 1;
    int64_t *queue = calloc(queueCapacity, sizeof(int64_t));
    if (!queue) {
        perror("calloc");
        exit(2);
    }
    size_t head = 0, depth = 0;
    uint64_t state = hashText(options->podId, options->phaseSeed ^ 0xa5a5a5a5ULL);

    sleepUntilMonotonic(startNs);
    while (!stopRequested) {
        int64_t nowNs = readClockNs(CLOCK_MONOTONIC);
        if (nowNs >= endNs) break;
        if (nextArrivalNs <= nowNs) {
            uint64_t due = 1 + (uint64_t)((nowNs - nextArrivalNs) / periodNs);
            for (uint64_t item = 0; item < due; item++) {
                int64_t arrival = nextArrivalNs + (int64_t)item * periodNs;
                if (arrival >= endNs) break;
                if (record) result->planned++;
                if (depth < queueCapacity) {
                    queue[(head + depth) % queueCapacity] = arrival;
                    depth++;
                    if (record && depth > result->maxQueueDepth) result->maxQueueDepth = depth;
                } else if (record) {
                    result->dropped++;
                }
            }
            nextArrivalNs += (int64_t)due * periodNs;
        }
        if (depth) {
            int64_t arrivalNs = queue[head];
            head = (head + 1) % queueCapacity;
            depth--;
            int64_t workStartNs = readClockNs(CLOCK_MONOTONIC);
            if (record) result->started++;
            state = executeWork(options->workUnits, state);
            int64_t workEndNs = readClockNs(CLOCK_MONOTONIC);
            if (record) {
                double startMs = (workStartNs - arrivalNs) / 1e6;
                double completionMs = (workEndNs - arrivalNs) / 1e6;
                result->completed++;
                if (completionMs <= options->deadlineMicroseconds / 1000.0) result->completedOnTime++;
                if (startMs > result->maxQueueAgeMs) result->maxQueueAgeMs = startMs;
                appendLatency(result, startMs, completionMs);
            }
        } else {
            int64_t target = nextArrivalNs < endNs ? nextArrivalNs : endNs;
            sleepUntilMonotonic(target);
        }
    }

    if (record && !stopRequested && nextArrivalNs < endNs) {
        uint64_t remaining = 1 + (uint64_t)((endNs - 1 - nextArrivalNs) / periodNs);
        result->planned += remaining;
        size_t available = queueCapacity - depth;
        size_t accepted = remaining < available ? (size_t)remaining : available;
        depth += accepted;
        result->dropped += remaining - accepted;
    }
    if (record) {
        result->pending += depth;
        result->cpuTimeMs += (readClockNs(CLOCK_THREAD_CPUTIME_ID) - cpuBefore) / 1e6;
    }
    free(queue);
}

static void printOptionalDouble(const char *name, double value) {
    printf("\"%s\":", name);
    if (isfinite(value)) printf("%.6f", value);
    else printf("null");
}

static void executeWorkload(const Options *options) {
    char hostname[256] = {0};
    if (gethostname(hostname, sizeof(hostname) - 1) != 0) strcpy(hostname, "unknown-pod");
    Options runtime = *options;
    if (!runtime.podId) runtime.podId = hostname;
    int64_t epochRealtimeNs = waitForEpoch(&runtime);
    int64_t realtimeNowNs = readClockNs(CLOCK_REALTIME);
    int64_t monotonicNowNs = readClockNs(CLOCK_MONOTONIC);
    int64_t epochMonotonicNs = monotonicNowNs + (epochRealtimeNs - realtimeNowNs);
    if (epochMonotonicNs < monotonicNowNs - 1000000000LL) {
        fprintf(stderr, "epoch is already more than one second in the past\n");
        exit(3);
    }

    Result result = {0};
    Result discarded = {0};
    executePeriod(&runtime, epochMonotonicNs, runtime.warmupSeconds, 0, &discarded);
    int64_t measurementStartNs = epochMonotonicNs + (int64_t)llround(runtime.warmupSeconds * 1e9);
    executePeriod(&runtime, measurementStartNs, runtime.durationSeconds, 1, &result);
    const char *status = stopRequested ? "terminated" : "success";
    int invariantOk = result.planned == result.completed + result.pending + result.dropped;
    double p50 = percentile(result.completionLatenciesMs, result.latencyCount, 0.50);
    double p95 = percentile(result.completionLatenciesMs, result.latencyCount, 0.95);
    double p99 = percentile(result.completionLatenciesMs, result.latencyCount, 0.99);
    double startP99 = percentile(result.startLatenciesMs, result.latencyCount, 0.99);

    printf("{\"mode\":\"workload\",\"status\":\"%s\",\"pod_id\":\"%s\",", status, runtime.podId);
    printf("\"jobs_per_second\":%.6f,\"work_units\":%" PRIu64 ",\"phase_seed\":%" PRIu64 ",",
           runtime.jobsPerSecond, runtime.workUnits, runtime.phaseSeed);
    printf("\"warmup_seconds\":%.6f,\"duration_seconds\":%.6f,\"deadline_us\":%.3f,",
           runtime.warmupSeconds, runtime.durationSeconds, runtime.deadlineMicroseconds);
    printf("\"planned_arrivals\":%" PRIu64 ",\"started\":%" PRIu64 ",\"completed\":%" PRIu64
           ",\"completed_on_time\":%" PRIu64 ",\"dropped\":%" PRIu64 ",\"pending_at_end\":%" PRIu64 ",",
           result.planned, result.started, result.completed, result.completedOnTime, result.dropped, result.pending);
    printf("\"count_conservation_ok\":%s,\"max_queue_depth\":%" PRIu64 ",\"max_queue_age_ms\":%.6f,",
           invariantOk ? "true" : "false", result.maxQueueDepth, result.maxQueueAgeMs);
    printOptionalDouble("latency_p50_ms", p50); printf(",");
    printOptionalDouble("latency_p95_ms", p95); printf(",");
    printOptionalDouble("latency_p99_ms", p99); printf(",");
    printOptionalDouble("start_latency_p99_ms", startP99); printf(",");
    printf("\"cpu_time_ms\":%.6f,\"work_sink\":%" PRIu64 "}\n", result.cpuTimeMs, workSink);
    free(result.completionLatenciesMs);
    free(result.startLatenciesMs);
    free(discarded.completionLatenciesMs);
    free(discarded.startLatenciesMs);
    if (!invariantOk) exit(4);
}

int main(int argc, char **argv) {
    signal(SIGTERM, handleSignal);
    signal(SIGINT, handleSignal);
    Options options = parseOptions(argc, argv);
    if (options.calibrateTargetMicroseconds > 0.0) calibrateWork(&options);
    else executeWorkload(&options);
    return 0;
}
