from trawl.core.audio import AudioTools
from trawl.tag.replaygain import album_gain, track_gain
from trawl.tag.writer import write
from trawl.verify.fingerprint import fingerprint
from trawl.verify.probe import decode_ok, probe


def default_audio_tools() -> AudioTools:
    return AudioTools(probe, decode_ok, fingerprint, write, track_gain, album_gain)
