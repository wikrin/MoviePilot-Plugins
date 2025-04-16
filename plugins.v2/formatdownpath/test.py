from app.plugins.formatdownpath import FormatDownPath
from app.core.event import Event
from app.schemas.types import EventType
from app.core.metainfo import MetaInfo
from app.core.context import  Context

# test_type = "cron"
test_type = "event"
if __name__ == "__main__":
    # 测试用例
    fdp = FormatDownPath()
    fdp.init_plugin()
    fdp._event_enabled = True
    fdp._cron_enabled = True
    fdp._rename_file = False
    fdp._rename_torrent = True
    fdp._downloader = ["local"]
    fdp._format_torrent_name = "{{ title }}{% if year %} ({{ year }}){% endif %}{% if season_episode %} - {{season_episode}}{% endif %}"
    fdp._format_save_path = "{{title}}{% if year %} ({{year}}){% endif %}"
    fdp._format_movie_path = "{% if season %}Season {{season}}/{% endif %}{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}"
    # 测试数据
    match test_type:
        case "event":
            downloader = fdp._downloader[0]
            hash = "684e92fa50d2e263dc8fbbd5d77cbc3fb045e930"
            meta_info = MetaInfo("[Nekomoe kissaten] Monogatari Series - Off & Monster Season [08][BDRip 1080p HEVC-10bit AACx2 ASSx4]")
            media_info = fdp.chain.recognize_media(meta_info)
            event_data = {
                "downloader": downloader, 
                "hash": hash,
                "context": Context(meta_info=meta_info, media_info=media_info)
                }
            event: Event = Event(EventType.DownloadAdded, event_data=event_data)
            fdp.event_process_main(event)
        case "cron":
            fdp.cron_process_main()
