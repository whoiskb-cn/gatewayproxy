package core

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/guohuiyuan/music-lib/model"
	"github.com/guohuiyuan/music-lib/soda"
	"github.com/guohuiyuan/music-lib/utils"
)

type DownloadedSong struct {
	Data        []byte
	Ext         string
	ContentType string
	Filename    string
	SavedPath   string
	Lyric       string // 新增：用于保存歌词文本
	Warning     string
}

func DownloadSongData(song *model.Song, withCover bool, withLyrics bool) (*DownloadedSong, error) {
	if song == nil {
		return nil, errors.New("song is nil")
	}
	if strings.TrimSpace(song.ID) == "" || strings.TrimSpace(song.Source) == "" {
		return nil, errors.New("missing song id or source")
	}

	normalized := *song
	normalized.Name = strings.TrimSpace(normalized.Name)
	normalized.Artist = strings.TrimSpace(normalized.Artist)
	if normalized.Name == "" {
		normalized.Name = "Unknown"
	}
	if normalized.Artist == "" {
		normalized.Artist = "Unknown"
	}

	audioData, contentType, err := fetchSongAudio(&normalized)
	if err != nil {
		return nil, err
	}

	ext := DetectAudioExt(audioData)
	if extByType := DetectAudioExtByContentType(contentType); extByType != "" {
		ext = extByType
	}

	var lyric string
	if withLyrics {
		if lyricFn := GetLyricFunc(normalized.Source); lyricFn != nil {
			lyric, _ = lyricFn(&model.Song{ID: normalized.ID, Source: normalized.Source, Extra: normalized.Extra})
		}
	}

	var coverData []byte
	var coverMime string
	if withCover && strings.TrimSpace(normalized.Cover) != "" {
		coverData, coverMime, _ = FetchBytesWithMime(normalized.Cover, normalized.Source)
	}

	finalData := audioData
	warning := ""
	if (ext == "mp3" || ext == "flac" || ext == "m4a" || ext == "wma") && (lyric != "" || len(coverData) > 0) {
		embeddedData, embedErr := EmbedSongMetadata(audioData, &normalized, lyric, coverData, coverMime)
		switch {
		case embedErr == nil:
			finalData = embeddedData
		case errors.Is(embedErr, ErrFFmpegNotFound):
			warning = "ffmpeg not found, metadata embedding skipped"
		default:
			warning = "metadata embedding failed, using original audio"
		}
	}

	if ext == "" {
		ext = DetectAudioExt(finalData)
	}

	return &DownloadedSong{
		Data:        finalData,
		Ext:         ext,
		ContentType: AudioMimeByExt(ext),
		Filename:    fmt.Sprintf("%s - %s.%s", normalized.Name, normalized.Artist, ext),
		Lyric:       lyric, // 保存抓取到的歌词
		Warning:     warning,
	}, nil
}

func SaveSongToFile(song *model.Song, outDir string, withCover bool, withLyrics bool) (*DownloadedSong, error) {
	result, err := DownloadSongData(song, withCover, withLyrics)
	if err != nil {
		return nil, err
	}

	targetDir := strings.TrimSpace(outDir)
	if targetDir == "" {
		targetDir = DefaultWebDownloadDir
	}
	targetDir = filepath.Clean(targetDir)

	name := "Unknown"
	artist := "Unknown"
	album := "Unknown"
	if song != nil && strings.TrimSpace(song.Name) != "" {
		name = strings.TrimSpace(song.Name)
	}
	if song != nil && strings.TrimSpace(song.Artist) != "" {
		artist = strings.TrimSpace(song.Artist)
	}
	if song != nil && strings.TrimSpace(song.Album) != "" {
		album = strings.TrimSpace(song.Album)
	}

	// 提取第一主唱作为目录名，避免多人合唱导致目录过长或分类散乱
	dirArtist := artist
	firstIdx := len(dirArtist)
	for _, sep := range []string{"/", "&", ",", "、", "|"} {
		if idx := strings.Index(dirArtist, sep); idx != -1 && idx < firstIdx {
			firstIdx = idx
		}
	}
	if firstIdx < len(dirArtist) {
		dirArtist = strings.TrimSpace(dirArtist[:firstIdx])
	}

	// 自动新建目录：主唱/专辑名称
	finalDir := filepath.Join(targetDir, utils.SanitizeFilename(dirArtist), utils.SanitizeFilename(album))

	if err := os.MkdirAll(finalDir, 0755); err != nil {
		return nil, err
	}

	fileName := fmt.Sprintf("%s - %s.%s", utils.SanitizeFilename(name), utils.SanitizeFilename(artist), result.Ext)
	filePath := filepath.Join(finalDir, fileName)
	if err := os.WriteFile(filePath, result.Data, 0644); err != nil {
		return nil, err
	}

	result.SavedPath = filePath

	// 如果有歌词，保存同名的 .lrc 文件
	if result.Lyric != "" {
		lrcPath := strings.TrimSuffix(filePath, filepath.Ext(filePath)) + ".lrc"
		_ = os.WriteFile(lrcPath, []byte(result.Lyric), 0644)
	}

	return result, nil
}

func fetchSongAudio(song *model.Song) ([]byte, string, error) {
	if song.Source == "soda" {
		cookie := CM.Get("soda")
		sodaInst := soda.New(cookie)
		info, err := sodaInst.GetDownloadInfo(song)
		if err != nil {
			return nil, "", err
		}

		encryptedData, _, err := FetchBytesWithMime(info.URL, "soda")
		if err != nil {
			return nil, "", err
		}

		finalData, err := soda.DecryptAudio(encryptedData, info.PlayAuth)
		if err != nil {
			return nil, "", err
		}
		return finalData, "", nil
	}

	dlFunc := GetDownloadFunc(song.Source)
	if dlFunc == nil {
		return nil, "", fmt.Errorf("unsupported source: %s", song.Source)
	}

	urlStr, err := dlFunc(song)
	if err != nil {
		return nil, "", err
	}
	if urlStr == "" {
		return nil, "", errors.New("empty download url")
	}

	return FetchBytesWithMime(urlStr, song.Source)
}
