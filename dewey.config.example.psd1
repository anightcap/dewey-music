# Dewey settings for the PowerShell scripts (dewey.ps1 and sync-music.ps1).
#
# 1. Copy this file to  dewey.config.psd1  (same folder). The copy is git-ignored: it never leaves your PC.
# 2. Replace the example values below with yours. Keep the quotes. Lines starting with # are comments.
#
# It is a plain data file (it cannot run code). A parameter on the command line always wins over this file,
# e.g.  .\dewey.ps1 scan -NasHost 192.168.1.60

@{
    # --- required -------------------------------------------------------------------------------------------
    # The NAS (or server) that runs Docker, and an account on it that may use ssh and sudo.
    NasHost   = '192.168.1.50'
    NasUser   = 'your-nas-user'

    # Dewey's folder on the NAS, and the same folder as your PC sees it (a mapped or UNC network share).
    RemoteDir = '/volume1/docker/dewey'
    Share     = '\\192.168.1.50\docker\dewey'

    # --- only for sync-music.ps1 (copying new music from this PC) -------------------------------------------
    MusicSource = 'C:\Users\you\Music'          # the folder on this PC that holds the music to send
    MediaShare  = '\\192.168.1.50\Media'        # the share that contains Music\ and _incoming\

    # --- optional (leave out, or leave '' for the default) --------------------------------------------------
    Media    = ''     # NAS folder that contains Music/ ; default /volume1/Media
    NdData   = ''     # NAS folder that holds navidrome.db, only if it is not found automatically
    BeetsDir = ''     # NAS folder that holds the Beets library.db, only for 'beets-remap' (e.g. /volume1/docker/beets)
    TimeZone = ''     # e.g. 'America/New_York'; used when the image is built; default UTC
    Label    = ''     # name shown in your authenticator app for the runner's secret; default 'console'
}
