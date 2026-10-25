#include <gst/gst.h>
#include <glib.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

namespace {

const std::string BLACK_PLAYLIST = "black.m3u8";

std::atomic<bool> g_stop{false};
std::atomic<bool> g_black_error{false};
std::string g_black_error_message;
GstElement *g_black_pipeline = nullptr;
std::mutex g_pipeline_mutex;
std::string g_hls_dir;
std::string g_live_playlist;
std::string g_black_playlist;
std::string g_black_segment_pattern;
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

std::string json_escape(const std::string &value) {
    std::string out;
    for (char c : value) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default: out.push_back(c); break;
        }
    }
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

std::string current_error_message() {
    return g_black_error.load() ? g_black_error_message : "";
}

std::string state_json(const std::string &status) {
    std::ostringstream os;
    os << "{\"status\":\"" << json_escape(status)
       << "\",\"hls_dir\":\"" << json_escape(g_hls_dir)
       << "\",\"playlist\":\"" << json_escape(g_live_playlist)
       << "\",\"error\":\"" << json_escape(current_error_message())
       << "\"}";
    return os.str();
}

void stop_pipeline(GstElement *&pipeline) {
    if (!pipeline) return;
    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_NULL);
    if (ret == GST_STATE_CHANGE_FAILURE) {
        std::cerr << "stop_pipeline: failed to set NULL" << std::endl;
    }
    gst_object_unref(pipeline);
    pipeline = nullptr;
}

GstBusSyncReply bus_sync_handler(GstBus *bus, GstMessage *msg, gpointer user_data) {
    if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
        GError *err = nullptr;
        gchar *dbg = nullptr;
        gst_message_parse_error(msg, &err, &dbg);
        g_black_error_message = err ? err->message : "unknown error";
        g_black_error.store(true);
        if (err) g_error_free(err);
        if (dbg) g_free(dbg);
    }
    return GST_BUS_PASS;
}

bool wait_for_playlist(const std::string &path, int timeout_ms) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        std::ifstream f(path);
        if (f.good() && f.peek() != std::ifstream::traits_type::eof()) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return false;
}

bool switch_live_symlink(const std::string &playlist_name) {
    std::string tmp = g_live_playlist + ".tmp";
    unlink(tmp.c_str());
    if (symlink(playlist_name.c_str(), tmp.c_str()) != 0) {
        g_black_error_message = "failed to create HLS symlink: " + std::string(strerror(errno));
        g_black_error.store(true);
        return false;
    }
    if (rename(tmp.c_str(), g_live_playlist.c_str()) != 0) {
        g_black_error_message = "failed to replace HLS playlist: " + std::string(strerror(errno));
        g_black_error.store(true);
        unlink(tmp.c_str());
        return false;
    }
    return true;
}

std::string black_pipeline_desc() {
    std::ostringstream os;
    os << "videotestsrc pattern=black is-live=true ! video/x-raw,width=" << g_width
       << ",height=" << g_height << ",framerate=25/1"
       << " ! videoconvert ! video/x-raw,format=I420"
       << " ! x264enc key-int-max=50 bitrate=6000 tune=zerolatency speed-preset=veryfast"
       << " ! h264parse config-interval=1 ! hls.video"
       << " audiotestsrc wave=silence is-live=true ! audio/x-raw,rate=48000,channels=2"
       << " ! avenc_aac bitrate=160000 ! hls.audio"
       << " hlssink2 name=hls playlist-location=" << quote_gst(g_black_playlist)
       << " location=" << quote_gst(g_black_segment_pattern)
       << " target-duration=" << g_target_duration
       << " max-files=" << g_max_files;
    return os.str();
}

bool start_black_pipeline_locked() {
    stop_pipeline(g_black_pipeline);
    g_black_error.store(false);
    g_black_error_message.clear();

    GError *err = nullptr;
    GstElement *pipeline = gst_parse_launch(black_pipeline_desc().c_str(), &err);
    if (!pipeline) {
        std::ostringstream os;
        if (err) os << err->message; else os << "black pipeline parse failed";
        g_black_error_message = os.str();
        g_black_error.store(true);
        if (err) g_error_free(err);
        return false;
    }

    GstBus *bus = gst_element_get_bus(pipeline);
    gst_bus_set_sync_handler(bus, bus_sync_handler, nullptr, nullptr);
    gst_object_unref(bus);

    g_black_pipeline = pipeline;
    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_FAILURE) {
        g_black_error_message = "failed to set black pipeline PLAYING";
        g_black_error.store(true);
        stop_pipeline(g_black_pipeline);
        return false;
    }

    bool ready = wait_for_playlist(g_black_playlist, 10000);
    if (!ready) {
        g_black_error_message = "black HLS playlist not ready within timeout";
        g_black_error.store(true);
        stop_pipeline(g_black_pipeline);
        return false;
    }

    return switch_live_symlink(BLACK_PLAYLIST);
}

void monitor_thread() {
    while (!g_stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        std::lock_guard<std::mutex> lock(g_pipeline_mutex);
        if (g_black_error.load()) {
            start_black_pipeline_locked();
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
        std::lock_guard<std::mutex> lock(g_pipeline_mutex);
        bool ok = start_black_pipeline_locked();
        std::cout << (ok ? "{\"status\":\"ok\",\"active\":\"black\"}" : state_json("error")) << std::endl;
        std::cout.flush();
        return;
    }
    const std::string switch_prefix = "SWITCH ";
    if (line.rfind(switch_prefix, 0) == 0) {
        std::string playlist = line.substr(switch_prefix.size());
        std::lock_guard<std::mutex> lock(g_pipeline_mutex);
        bool ok = switch_live_symlink(playlist);
        std::cout << (ok ? "{\"status\":\"ok\"}" : state_json("error")) << std::endl;
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
    g_live_playlist = g_hls_dir + "/live.m3u8";
    g_black_playlist = g_hls_dir + "/" + BLACK_PLAYLIST;
    g_black_segment_pattern = g_hls_dir + "/black_%05d.ts";
    g_target_duration = env_int("GSTREAMER_TARGET_DURATION", 2);
    g_max_files = env_int("GSTREAMER_MAX_FILES", 45);
    g_width = env_int("GSTREAMER_WIDTH", 1280);
    g_height = env_int("GSTREAMER_HEIGHT", 720);
    ensure_dir(g_hls_dir);

    std::thread monitor(monitor_thread);
    {
        std::lock_guard<std::mutex> lock(g_pipeline_mutex);
        start_black_pipeline_locked();
    }

    std::string line;
    while (std::getline(std::cin, line)) {
        if (g_stop.load()) break;
        handle_command(line);
    }

    g_stop.store(true);
    monitor.join();
    {
        std::lock_guard<std::mutex> lock(g_pipeline_mutex);
        stop_pipeline(g_black_pipeline);
    }
    gst_deinit();
    return 0;
}
