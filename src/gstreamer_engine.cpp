#include <gst/gst.h>
#include <glib.h>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <vector>

namespace {

std::atomic<bool> g_stop{false};
std::atomic<bool> g_pipeline_error{false};
std::string g_error_message;
std::string g_active_label = "black";
GstElement *g_pipeline = nullptr;
std::mutex g_pipeline_mutex;
std::string g_hls_dir;
std::string g_playlist_path;
std::string g_segment_pattern;
int g_target_duration = 2;
int g_max_files = 45;
int g_width = 1280;
int g_height = 720;

void ensure_dir(const std::string &path) {
    if (path.empty()) return;
    mkdir(path.c_str(), 0755);
}

std::string quote_gst(const std::string &value) {
    std::string out = "\"";
    for (char c : value) {
        if (c == '\\' || c == '"') out.push_back('\\');
        out.push_back(c);
    }
    out.push_back('"');
    return out;
}

std::string env_or(const char *name, const std::string &fallback) {
    const char *value = std::getenv(name);
    return value && *value ? std::string(value) : fallback;
}

int env_int(const char *name, int fallback) {
    const char *value = std::getenv(name);
    if (!value || !*value) return fallback;
    try { return std::stoi(value); } catch (...) { return fallback; }
}

std::string state_json(const std::string &status) {
    std::ostringstream os;
    os << "{\"status\":\"" << status << "\",\"active\":\"" << g_active_label
       << "\",\"hls_dir\":\"" << g_hls_dir << "\",\"playlist\":\"" << g_playlist_path
       << "\",\"error\":\"" << (g_pipeline_error ? g_error_message : "") << "\"}";
    return os.str();
}

void stop_current_pipeline_locked() {
    if (g_pipeline) {
        gst_element_set_state(g_pipeline, GST_STATE_NULL);
        gst_object_unref(g_pipeline);
        g_pipeline = nullptr;
    }
}

GstBusSyncReply bus_sync_handler(GstBus *, GstMessage *message, gpointer) {
    if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR) {
        GError *err = nullptr;
        gchar *dbg = nullptr;
        gst_message_parse_error(message, &err, &dbg);
        std::ostringstream os;
        if (err) os << err->message;
        if (dbg) os << " (" << dbg << ")";
        g_error_message = os.str();
        g_pipeline_error.store(true);
        if (err) g_error_free(err);
        if (dbg) g_free(dbg);
    }
    return GST_BUS_PASS;
}

std::string source_pipeline_desc(const std::string &url) {
    bool is_hls = url.find(".m3u8") != std::string::npos || url.find("manifest/hls") != std::string::npos;
    std::string demux = is_hls ? "hlsdemux name=demux" : "tsdemux name=demux";
    std::ostringstream os;
    os << "souphttpsrc location=" << quote_gst(url)
       << " timeout=10 is-live=true do-timestamp=false ! "
       << demux
       << " demux. ! queue leaky=downstream ! decodebin ! videoconvert ! videoscale"
       << " ! video/x-raw,width=" << g_width << ",height=" << g_height
       << ",framerate=25/1,format=I420"
       << " ! x264enc key-int-max=50 bitrate=6000 tune=zerolatency speed-preset=veryfast"
       << " ! h264parse config-interval=1 ! hls."
       << " demux. ! queue leaky=downstream ! decodebin ! audioconvert ! audioresample"
       << " ! audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=160000 ! hls."
       << " hlssink2 name=hls playlist-location=" << quote_gst(g_playlist_path)
       << " location=" << quote_gst(g_segment_pattern)
       << " target-duration=" << g_target_duration
       << " max-files=" << g_max_files;
    return os.str();
}

std::string black_pipeline_desc() {
    std::ostringstream os;
    os        << "videotestsrc pattern=black is-live=true ! video/x-raw,width=" << g_width
       << ",height=" << g_height << ",framerate=25/1"
       << " ! videoconvert ! video/x-raw,format=I420"
       << " ! x264enc key-int-max=50 bitrate=6000 tune=zerolatency speed-preset=veryfast"
       << " ! h264parse config-interval=1 ! hls."
       << " audiotestsrc wave=silence is-live=true ! audio/x-raw,rate=48000,channels=2"
       << " ! avenc_aac bitrate=160000 ! hls."
       << " hlssink2 name=hls playlist-location=" << quote_gst(g_playlist_path)
       << " location=" << quote_gst(g_segment_pattern)
       << " target-duration=" << g_target_duration
       << " max-files=" << g_max_files;
    return os.str();
}

bool wait_for_playlist(int timeout_ms) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        std::ifstream f(g_playlist_path);
        if (f.good() && f.peek() != std::ifstream::traits_type::eof()) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return false;
}

bool start_pipeline(const std::string &label, const std::string &url) {
    std::lock_guard<std::mutex> lock(g_pipeline_mutex);
    g_pipeline_error.store(false);
    g_error_message.clear();
    g_active_label = label;
    stop_current_pipeline_locked();

    std::string desc = label == "black" ? black_pipeline_desc() : source_pipeline_desc(url);
    GError *err = nullptr;
    GstElement *pipeline = gst_parse_launch(desc.c_str(), &err);
    if (!pipeline) {
        std::ostringstream os;
        if (err) os << err->message; else os << "pipeline parse failed";
        g_error_message = os.str();
        g_pipeline_error.store(true);
        if (err) g_error_free(err);
        return false;
    }

    GstBus *bus = gst_element_get_bus(pipeline);
    gst_bus_set_sync_handler(bus, bus_sync_handler, nullptr, nullptr);
    gst_object_unref(bus);

    g_pipeline = pipeline;
    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_FAILURE) {
        g_error_message = "failed to set pipeline PLAYING";
        g_pipeline_error.store(true);
        stop_current_pipeline_locked();
        return false;
    }
    bool ready = wait_for_playlist(10000);
    if (!ready) {
        g_error_message = "HLS playlist not ready within timeout";
        g_pipeline_error.store(true);
    }
    return ready;
}

void monitor_thread() {
    while (!g_stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        if (g_pipeline_error.load() && g_active_label != "black") {
            start_pipeline("black", "");
        }
    }
}

void handle_command(const std::string &line) {
    if (line == "STOP") {
        g_stop.store(true);
        return;
    }
    if (line == "STATUS") {
        std::cout << state_json("ok") << std::endl;
        std::cout.flush();
        return;
    }
    if (line == "BLACK") {
        bool ok = start_pipeline("black", "");
        std::cout << (ok ? "{\"status\":\"ok\",\"active\":\"black\"}" : state_json("error")) << std::endl;
        std::cout.flush();
        return;
    }
    const std::string prefix = "SOURCE ";
    if (line.rfind(prefix, 0) == 0) {
        std::string url = line.substr(prefix.size());
        bool ok = start_pipeline("source", url);
        std::cout << (ok ? "{\"status\":\"ok\",\"active\":\"source\"}" : state_json("error")) << std::endl;
        std::cout.flush();
        return;
    }
    std::cout << "{\"status\":\"error\",\"error\":\"unknown command\"}" << std::endl;
    std::cout.flush();
}

} // namespace

int main(int argc, char **argv) {
    gst_init(&argc, &argv);

    g_hls_dir = env_or("GSTREAMER_HLS_DIR", env_or("PROGRAM_HLS_DIR", "/tmp/gstreamer-hls"));
    g_playlist_path = g_hls_dir + "/live.m3u8";
    g_segment_pattern = g_hls_dir + "/seg_%05d.ts";
    g_target_duration = env_int("GSTREAMER_TARGET_DURATION", 2);
    g_max_files = env_int("GSTREAMER_MAX_FILES", 45);
    g_width = env_int("GSTREAMER_WIDTH", 1280);
    g_height = env_int("GSTREAMER_HEIGHT", 720);
    ensure_dir(g_hls_dir);

    std::thread monitor(monitor_thread);
    start_pipeline("black", "");

    std::string line;
    while (std::getline(std::cin, line)) {
        if (g_stop.load()) break;
        handle_command(line);
    }

    g_stop.store(true);
    monitor.join();
    {
        std::lock_guard<std::mutex> lock(g_pipeline_mutex);
        stop_current_pipeline_locked();
    }
    gst_deinit();
    return 0;
}
