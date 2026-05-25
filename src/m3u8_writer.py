from typing import List, Dict, Any

class M3U8Writer:
    """
    Class to write stream information into an .m3u8 file.
    """

    def __init__(self, output_path: str):
        self.output_path = output_path

    def write(self, streams: List[Dict[str, Any]]):
        """
        Writes the list of streams to the specified output path in m3u8 format.
        """
        with open(self.output_path, 'w') as f:
            f.write("#EXTM3U\n")
            for stream in streams:
                f.write(f"#EXTINF:-1, {stream['title']} ({stream['resolution']})\n")
                f.write(f"{stream['url']}\n")
        
        return True
