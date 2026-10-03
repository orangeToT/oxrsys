# SPDX-License-Identifier: MPL-2.0

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description="Run isolated checks on Quest client function bodies with fake platform APIs.")
parser.add_argument('kind', choices=['image', 'pose', 'input'])
parser.add_argument('repository', type=pathlib.Path)
parser.add_argument('--revision', help='Read this Git revision instead of the working tree')
parser.add_argument('--base', default='f72f2b20dd0c9e5a4c0b43ca7f9b0f65704aa1e6')
parser.add_argument('--output', type=pathlib.Path)
args = parser.parse_args()
kind = args.kind
version = args.revision or 'working-tree'
repo = args.repository.resolve()
out = args.output.resolve() if args.output else pathlib.Path(tempfile.mkdtemp(prefix='quest-repro-'))
out.mkdir(parents=True, exist_ok=True)
print('Results: ' + str(out), flush=True)
prefix = 'clients/android-vr/app/src/main/cpp/'

def source(name):
    if args.revision:
        return subprocess.check_output(['git', 'show', args.revision + ':' + prefix + name], cwd=repo, text=True)
    return (repo / prefix / name).read_text()

def block(text, marker):
    start = text.index(marker)
    brace = text.index('{', start)
    depth = 1
    pos = brace + 1
    while depth:
        depth += (text[pos] == '{') - (text[pos] == '}')
        pos += 1
    return text[start:pos]

common = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>
#include <oxrsys/protocol/Protocol.h>
namespace protocol = oxr::protocol;
#define LOGE(...) ((void)0)
#define LOGI(...) ((void)0)
#define LOGW(...) ((void)0)
int failed = 0, checked = 0;
#define CHECK(x) do { ++checked; if (!(x)) { ++failed; std::cerr << "FAIL " << #x << '\n'; } } while (0)
auto testNow = std::chrono::steady_clock::time_point(std::chrono::seconds(10));
int64_t SteadyClockNowNs() { return testNow.time_since_epoch().count(); }
int finish() { std::cout << checked << " checks, " << failed << " failures\n"; return failed ? 1 : 0; }
'''

decoder = source('VideoDecoder.cpp')
app = source('XrApp.cpp')
parts = []

if kind == 'image':
    method = block(decoder, 'bool VideoDecoder::AcquireFrame(')
    frame = block(source('VideoDecoder.h'), 'struct DecodedFrame') + ';'
    decl = method[:method.index('{')] + ';'
    decl = decl.replace('VideoDecoder::', '')
    parts = [method]
    prelude = r'''
using media_status_t = int;
constexpr int AMEDIA_OK = 0;
struct AHardwareBuffer {} hardware;
struct AHardwareBuffer_Desc { uint32_t width=10,height=10,stride=10,layers=1,format=1; uint64_t usage=0; };
struct AImageCropRect { int32_t left=0,top=0,right=10,bottom=10; };
struct AImage { int id; bool released=false, valid=true; };
struct AMediaCodec {} codec;
struct AImageReader {} reader;
AImage* replacement = nullptr;
int acquireStatus = 0, barriers = 0;
std::vector<std::string> events;
int AImageReader_acquireLatestImage(AImageReader*, AImage** out) {
    events.push_back("acquire"); *out = replacement; return acquireStatus;
}
int AImage_getHardwareBuffer(AImage* i, AHardwareBuffer** out) {
    events.push_back("buffer"); *out = i->valid ? &hardware : nullptr; return i->valid ? 0 : -1;
}
void AImage_delete(AImage* i) { events.push_back("delete:" + std::to_string(i->id)); i->released = true; }
void barrier() { ++barriers; events.push_back("barrier"); }
int AImage_getTimestamp(AImage*, int64_t* n) { *n=1000; return 0; }
int AImage_getWidth(AImage*, int32_t* n) { *n=10; return 0; }
int AImage_getHeight(AImage*, int32_t* n) { *n=10; return 0; }
int AImage_getCropRect(AImage*, AImageCropRect* r) { *r={}; return 0; }
void AHardwareBuffer_describe(AHardwareBuffer*, AHardwareBuffer_Desc* d) { *d={}; }
struct VideoDecoder {
FRAME
DECL
    struct PendingFrameMetadata { int64_t receiveTimeNs=0,submitTimeNs=0; bool alphaBlend=false; };
    bool ConsumeSubmittedFrameMetadata(int64_t, PendingFrameMetadata*) { return false; }
    AMediaCodec* codec_ = &codec;
    AImageReader* imageReader_ = &reader;
    AImage* currentImage_ = nullptr;
    std::atomic<uint32_t> outputFramesReleasedSinceAcquire_{0}, skippedFramesBeforeAcquire_{0};
    uint32_t width_=10,height_=10;
};
'''.replace('FRAME', frame).replace('DECL', decl)
    call = 'd.AcquireFrame(&f, barrier)' if 'beforeReleasePrevious' in method else 'd.AcquireFrame(&f)'
    tests = r'''
int main() {
    {
        VideoDecoder d; AImage old{1}; d.currentImage_=&old;
        VideoDecoder::DecodedFrame f; replacement=nullptr; acquireStatus=-1;
        d.outputFramesReleasedSinceAcquire_.store(3);
        for (int i=0;i<100;++i) CHECK(!CALL);
        CHECK(d.currentImage_==&old); CHECK(!old.released); CHECK(barriers==0);
        CHECK(d.outputFramesReleasedSinceAcquire_.load()==3);
    }
    {
        VideoDecoder d; AImage old{1}, bad{2,false,false}; d.currentImage_=&old;
        VideoDecoder::DecodedFrame f; replacement=&bad; acquireStatus=0;
        CHECK(!CALL); CHECK(d.currentImage_==&old); CHECK(!old.released); CHECK(bad.released); CHECK(barriers==0);
    }
    {
        VideoDecoder d; AImage next{2}; VideoDecoder::DecodedFrame f;
        replacement=&next; events.clear(); barriers=0;
        CHECK(CALL); CHECK(d.currentImage_==&next); CHECK(!next.released); CHECK(barriers==0);
    }
    {
        VideoDecoder d; AImage old{1}, next{2}; VideoDecoder::DecodedFrame f;
        d.currentImage_=&old; replacement=&next; events.clear(); barriers=0;
        d.outputFramesReleasedSinceAcquire_.store(3);
        CHECK(CALL); CHECK(old.released); CHECK(!next.released); CHECK(d.currentImage_==&next);
        CHECK(barriers==1); CHECK(f.skippedFramesBeforeAcquire==2);
        const std::vector<std::string> expected{"acquire","buffer","barrier","delete:1"};
        CHECK(events==expected);
    }
    return finish();
}
'''.replace('CALL', call)
    program = common + prelude + method + tests

elif kind == 'pose':
    stale = block(block(app, 'bool XrApp::RenderFrame('), 'else if (hasVideoTexture_)')
    parts = [stale]
    stale = stale.removeprefix('else ').replace('std::chrono::steady_clock::now()', 'testNow')
    prelude = r'''
struct XrApp {
    bool hasVideoTexture_=true, hasCurrentRenderPose_=true, disconnected=false;
    int currentRenderPose_=42;
    protocol::ClientReprojectionMode clientReprojectionMode_=protocol::ClientReprojectionMode::Pose;
    struct Presented { bool valid=true,hasRenderPose=true; int renderPose=42; uint32_t consecutiveReuses=0; int64_t localReceiveTimeNs=0; } presentedVideoFrame_;
    uint32_t staleFrameReusesSinceLastReport_=0,reprojectedFramesSinceLastReport_=0;
    std::chrono::steady_clock::time_point lastVideoFrameTime_;
    void OnConnectionLost(const char*) { disconnected=true; }
    bool Reuse() {
        bool hasVideo=false,reusingPresentedFrame=false;
STALE
        return hasVideo;
    }
};
'''.replace('STALE', stale)
    tests = r'''
int main() {
    for (int gap : {0,119,120,121,500,1999,2000,2500}) {
        XrApp a; a.lastVideoFrameTime_=testNow-std::chrono::milliseconds(gap);
        a.presentedVideoFrame_.localReceiveTimeNs=SteadyClockNowNs()-int64_t(gap)*1000000;
        CHECK(a.Reuse()==(gap<2000)); CHECK(a.disconnected==(gap>=2000));
        CHECK(a.hasCurrentRenderPose_==(gap<2000));
        if (gap<2000) CHECK(a.currentRenderPose_==42);
    }
    for (bool off : {false,true}) {
        XrApp a; a.lastVideoFrameTime_=testNow-std::chrono::milliseconds(500);
        a.presentedVideoFrame_.localReceiveTimeNs=SteadyClockNowNs()-500000000;
        a.presentedVideoFrame_.hasRenderPose=off;
        a.clientReprojectionMode_=off ? protocol::ClientReprojectionMode::Off : protocol::ClientReprojectionMode::Pose;
        CHECK(a.Reuse()); CHECK(!a.hasCurrentRenderPose_);
    }
    return finish();
}
'''
    base = subprocess.check_output(['git','show',args.base+':'+prefix+'XrApp.cpp'],cwd=repo,text=True)
    assert block(base, 'void XrApp::UpdateReprojectionWarp(') == block(app, 'void XrApp::UpdateReprojectionWarp(')
    program = common + prelude + tests

elif kind == 'input':
    submit = block(decoder, 'bool VideoDecoder::SubmitNalUnit(')
    on_nal = block(block(app, 'void XrApp::OnNalUnitReceived('), 'if (videoDecoder_ && videoDecoder_->IsInitialized())')
    recovery = block(block(app, 'void XrApp::RunFrame('), 'if (IsConnected() && networkReceiver_)')
    request = block(app, 'void XrApp::RequestKeyframe(')
    parts = [submit,on_nal,recovery,request]
    recovery=recovery.replace('std::chrono::steady_clock::now()', 'testNow')
    request=request.replace('std::chrono::steady_clock::now()', 'testNow')
    prelude = r'''
using media_status_t=int;
constexpr int AMEDIA_OK=0,MSG_DONTWAIT=0;
struct AMediaCodec {} codec;
int inputIndex=0,queueStatus=0,queueCalls=0;
size_t capacity=64;
bool nullBuffer=false;
uint8_t storage[64];
int AMediaCodec_dequeueInputBuffer(AMediaCodec*, int64_t timeout) { CHECK(timeout==0); return inputIndex; }
uint8_t* AMediaCodec_getInputBuffer(AMediaCodec*, int, size_t* size) { *size=capacity; return nullBuffer ? nullptr : storage; }
int AMediaCodec_queueInputBuffer(AMediaCodec*, int, size_t, size_t, int64_t, uint32_t) { ++queueCalls; return queueStatus; }
struct VideoDecoder {
    AMediaCodec* codec_=&codec;
    int remembered=0;
    bool IsInitialized() { return true; }
    void RememberSubmittedFrame(int64_t,int64_t,int64_t,bool) { ++remembered; }
    bool SubmitNalUnit(const uint8_t*,size_t,int64_t,int64_t,bool);
};
std::vector<protocol::RequestKeyframe> sent;
int send(int,const void* p,size_t n,int) { sent.push_back(*static_cast<const protocol::RequestKeyframe*>(p)); return n; }
bool SendTcpRecord(int,protocol::TcpRecordType,const void* p,size_t n) { send(0,p,n,0); return true; }
struct Network { uint32_t dropped=0; uint32_t GetFramesDropped() { return dropped; } } network;
struct XrApp {
    enum class TransportMode { Wifi, UsbAdbTcp } transportMode_=TransportMode::Wifi;
    int controlSocket_=1,controlTcpSocket_=1;
    std::atomic<uint32_t> pendingDecoderSubmitFailures_{0};
    uint32_t nalUnitsReceived_=100,lastObservedDroppedFrames_=0;
    bool hasVideoTexture_=true,connected=true;
    VideoDecoder decoder;
    VideoDecoder* videoDecoder_=&decoder;
    Network* networkReceiver_=&network;
    std::chrono::steady_clock::time_point lastKeyframeRequestTime_=testNow-std::chrono::seconds(1),lastVideoFrameTime_=testNow;
    bool IsConnected() { return connected; }
    void RequestKeyframe(uint32_t,uint32_t);
    void OnNal(const uint8_t* data,size_t size,int64_t timestampNs,int64_t receiveTimeNs,uint8_t flags) {
ON_NAL
    }
    void Recover() {
RECOVERY
    }
};
'''.replace('ON_NAL',on_nal).replace('RECOVERY',recovery)
    tests = r'''
int main() {
    uint8_t payload[]{1,2,3};
    {
        VideoDecoder d; inputIndex=-1; CHECK(!d.SubmitNalUnit(payload,3,1,1,false)); CHECK(d.remembered==0);
        inputIndex=0; capacity=1; CHECK(!d.SubmitNalUnit(payload,3,1,1,false)); CHECK(d.remembered==0);
        capacity=64; nullBuffer=true; CHECK(!d.SubmitNalUnit(payload,3,1,1,false)); CHECK(d.remembered==0);
        nullBuffer=false; queueStatus=-10000; CHECK(!d.SubmitNalUnit(payload,3,1,1,false)); CHECK(d.remembered==0);
    }
    {
        VideoDecoder d; queueStatus=0; CHECK(d.SubmitNalUnit(payload,3,1,1,false)); CHECK(d.remembered==1); CHECK(storage[2]==3);
    }
    for (bool usb : {false,true}) {
        XrApp a; sent.clear(); network.dropped=0; queueStatus=-10000;
        if (usb) a.transportMode_=XrApp::TransportMode::UsbAdbTcp;
        a.OnNal(payload,3,1000,1000,0); a.OnNal(payload,3,2000,1000,0);
        CHECK(a.pendingDecoderSubmitFailures_.load()==2);
        a.Recover(); CHECK(sent.size()==1); CHECK(a.pendingDecoderSubmitFailures_.load()==0);
        if (!sent.empty()) { CHECK(sent.back().detail==2); CHECK(sent.back().reasonFlags==protocol::KEYFRAME_REASON_FRAME_LOSS); }
        a.OnNal(payload,3,3000,1000,0);
        testNow+=std::chrono::milliseconds(99); a.lastVideoFrameTime_=testNow;
        a.Recover(); CHECK(sent.size()==1); CHECK(a.pendingDecoderSubmitFailures_.load()==1);
        testNow+=std::chrono::milliseconds(1); a.Recover(); CHECK(sent.size()==2); CHECK(a.pendingDecoderSubmitFailures_.load()==0);
        a.Recover(); CHECK(sent.size()==2);
        queueStatus=0; a.OnNal(payload,3,4000,1000,0); CHECK(a.pendingDecoderSubmitFailures_.load()==0);
        a.Recover(); CHECK(sent.size()==2);
    }
    {
        XrApp a; sent.clear(); queueStatus=0; network.dropped=1;
        a.Recover(); CHECK(sent.size()==1); CHECK(sent.back().reasonFlags==protocol::KEYFRAME_REASON_FRAME_LOSS);
        network.dropped=0; a.lastObservedDroppedFrames_=0; testNow+=std::chrono::milliseconds(200);
        a.Recover(); CHECK(sent.size()==2); CHECK(sent.back().reasonFlags==protocol::KEYFRAME_REASON_DECODE_STALL);
    }
    return finish();
}
'''
    program = common + prelude + submit + request + tests
else:
    raise ValueError(kind)

(out/'extracted.cpp').write_text('\n\n'.join(parts)+'\n')
(out/'test.cpp').write_text(program)
cmd=['clang++','-std=c++20','-O0','-g','-fsanitize=address,undefined','-I',str(repo/'common/protocol/include'),str(out/'test.cpp'),'-o',str(out/'test')]
compiled=subprocess.run(cmd,capture_output=True,text=True)
(out/'compile.log').write_text(compiled.stdout+compiled.stderr)
if compiled.returncode:
    print(compiled.stderr); sys.exit(compiled.returncode)
result=subprocess.run([str(out/'test')],capture_output=True,text=True)
(out/'test.log').write_text(result.stdout+result.stderr)
record={'kind':kind,'version':version,'source_base':args.base,'compile_command':cmd,'compile_exit_code':compiled.returncode,'test_exit_code':result.returncode,'source_sha256':{'VideoDecoder.cpp':hashlib.sha256(decoder.encode()).hexdigest(),'XrApp.cpp':hashlib.sha256(app.encode()).hexdigest()},'scope':'Actual function or branch bodies compiled with fake Android APIs and deterministic time. This does not execute Android MediaCodec, GLES, or an OpenXR headset.'}
(out/'result.json').write_text(json.dumps(record,indent=2)+'\n')
print(result.stdout+result.stderr);sys.exit(result.returncode)
