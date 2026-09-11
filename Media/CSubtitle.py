from pathlib import Path
from typing import Union, Dict, Any, List
import pysrt
import pandas as pd

from Common.CUtils import (
    is_audio,
    is_video,
    format_timestamp,
    time_to_seconds,
    data_frame_from_csv
)
from Common.EEnums import EActionType, EMediaType
from Common.CWhisperModel import CWhisperModel
from Media.CMedia import CMedia
from Media.CVideo import CVideo

class CSubtitle(CMedia):
    def __init__(
        self, json_path: Union[str, Path], action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Subtitle, action_type)
        self.whisper_model = CWhisperModel(logger=self.logger, model_size_or_path="large-v3-turbo")
        self.max_worker = self.whisper_model.max_parallel_worker
        self.mapping_video_srt_files: Dict[Path, List[Path]] = {}
        self.tolerance_seconds = 0.2
        
    def read(self) -> None:
        """Reads video/audio streams and metadata, exporting them as CSV files."""
        CVideo(self.json_path, self.action_type, EMediaType.Subtitle).read()
    
    def transcribe(self) -> None:
        """Runs parallel transcription of targeted videos, generates .srt files, and exports metadata DataFrames to CSV."""
        data = self.process_files_parallel(self._transcribe_video_audio_file)
        dfs = self.build_dataframes_by_codec(data, log_message="Transcribed ")
        self.export_dataframes_to_csv_by_codec(dfs, isOtherSource=True)
            
    def transcribeAndCheck(self) -> None:
        """Runs parallel transcription and checks synchronization of generated .srt files against existing ones."""
        self.mapping_video_srt_files = self.get_mapping_video_srt_files()
        data = self.process_files_parallel(self._check_cross_lingual_synchronization)
        dfs = self.build_dataframes_by_codec(data, log_message="Checked ")
        self.export_dataframes_to_csv_by_codec(dfs)
    
    def get_mapping_video_srt_files(self) -> Dict[Path, List[Path]]:
        """Returns the mapping of video files to their corresponding .srt files."""
        df = data_frame_from_csv(self.other_source_path, self.data_separator)
        
        return {
            Path(file_path).resolve(): [Path(srt).resolve() for srt in srt_list]
            for file_path, srt_list in df.groupby("file_path")["srt_path"].apply(list).to_dict().items()
        }

    def _transcribe_video_audio_file(
        self, 
        file_path: Union[str, Path]
        ) -> Dict[str, List[Dict[str, Any]]]:
        """Transcribes an individual video or audio file, generates its .srt file, and extracts attributes."""
        path_obj = Path(file_path)
        segmentData: List[Dict[str, Any]] = []
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_audio(path_obj)):
            return result
        
        target_dir = self.target_path

        try:
            srt_path = target_dir / f"{path_obj.stem}.srt"
            
            segmentData = self.logger.logElapsedTime(
                lambda: self.whisper_model.get_file_transcription_data(path_obj),
                f"Processing {path_obj.name} to get transcription data..."
            )
            
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(segmentData, start=1):
                    start_str = format_timestamp(segment["start"])
                    end_str = format_timestamp(segment["end"])
                    f.write(f"{i}\n{start_str} --> {end_str}\n{segment['text'].strip()}\n\n")
            
            result = {"subtitleMapping": [{"file_path": str(path_obj), "srt_path": str(srt_path)}]}
            self.logger.log(f"Transcription successful: {path_obj.name} -> Saved to {srt_path.name}")
            
        except Exception as ex:
            self.logger.logError(f"Error transcribing {path_obj.name}: {ex}")
                
        return result

    def _check_cross_lingual_synchronization(
        self, 
        file_path: Path
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Checks synchronization between a foreign language video and French SRT files
        by comparing VAD speech intervals against subtitle timestamps.
        """
        file_path_obj = Path(file_path).resolve()
        srt_path_objs = self.mapping_video_srt_files.get(file_path_obj, [])
        segmentData: List[Dict[str, Any]] = []
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not any(srt_path_objs):
            self.logger.logError(f"No corresponding SRT file found for {file_path_obj.name}. Skipping synchronization check.")
            return result

        segmentData = self.logger.logElapsedTime(
            lambda: self.whisper_model.get_file_transcription_data(file_path_obj, voice_activity_detection_filter=True),
            f"Processing {file_path_obj.name} to extract VAD speech intervals..."
        )
        
        speech_intervals = [{"start": seg["start"], "end": seg["end"]} for seg in segmentData if seg["text"].strip()]

        if not speech_intervals:
            self.logger.logError(f"No speech detected in {file_path_obj.name}.")
            return result

        all_reports = []
        for srt_path_obj in srt_path_objs:
            self.logger.log(f"Opening subtitle file: {srt_path_obj.name}...")
            srts = pysrt.open(srt_path_obj, encoding="utf-8")
            
            for sub in srts:
                sub_start = time_to_seconds(sub.start)
                sub_end = time_to_seconds(sub.end)
                
                closest_speech = min(
                    speech_intervals,
                    key=lambda s: abs(s["start"] - sub_start)
                )
                
                time_diff = sub_start - closest_speech["start"]
                
                is_during_speech = any(
                    s["start"] <= sub_start <= s["end"] or s["start"] <= sub_end <= s["end"]
                    for s in speech_intervals
                )

                all_reports.append({
                    "file_path": str(file_path_obj),
                    "srt_path": str(srt_path_obj),
                    "codec_type": "subtitle",
                    "subtitle_index": sub.index,
                    "subtitle_start": sub_start,
                    "speech_start_detected": closest_speech["start"],
                    "time_difference_seconds": time_diff,
                    "is_overlapping_speech": is_during_speech,
                    "subtitle_text_sample": f"{sub.text.replace(chr(10), ' ')[:30]}..."
                })
        
        df = pd.DataFrame(all_reports)

        if not df.empty:
            avg_drift = df["time_difference_seconds"].mean()
            orphaned_subs = df[~df["is_overlapping_speech"]].shape[0]
            
            self.logger.log("--- Cross-Lingual Synchronization Report ---")
            self.logger.log(f"Estimated average offset (Mean Drift): {avg_drift:.2f} seconds")
            self.logger.log(f"Subtitles falling during silences: {orphaned_subs} out of {len(df)}")
            
            df_drift_detected = df[df["time_difference_seconds"].abs() > self.tolerance_seconds]
            if df_drift_detected.empty:
                self.logger.log("✅ No significant sync drift detected across analyzed subtitles!")
            else:
                self.logger.log("⚠️ Significant offsets detected in subtitles. Review discrepancies below:")
                self.logger.log(df_drift_detected.to_string(index=False))
        else:
            self.logger.log("No subtitle records found for synchronization check.")

        result = {"crossLingualSynchronization": df.to_dict("records")}
        return result