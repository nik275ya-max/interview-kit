package main

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

//go:embed web/*
var webFiles embed.FS

type Segment struct {
	ID    string  `json:"id"`
	Start float64 `json:"start"`
	End   float64 `json:"end"`
	Text  string  `json:"text"`
	Words []Word  `json:"words,omitempty"`
}
type Word struct {
	Text  string  `json:"text"`
	Start float64 `json:"start"`
	End   float64 `json:"end"`
}
type Item struct {
	ID         string  `json:"id"`
	Kind       string  `json:"kind"`
	SourcePath string  `json:"source_path"`
	Start      float64 `json:"start"`
	End        float64 `json:"end"`
	Text       string  `json:"text,omitempty"`
	Words      []Word  `json:"words,omitempty"`
}
type Project struct {
	Name     string    `json:"name"`
	Source   string    `json:"source"`
	Segments []Segment `json:"segments"`
	Items    []Item    `json:"items"`
	Folder   string    `json:"folder"`
}
type App struct {
	root   string
	ffmpeg string
	mu     sync.RWMutex
	jobMu  sync.Mutex
	job    *TranscriptionJob
}

type TranscriptionJob struct {
	ID       string   `json:"id"`
	Status   string   `json:"status"`
	Stage    string   `json:"stage"`
	Progress int      `json:"progress"`
	Error    string   `json:"error,omitempty"`
	Project  *Project `json:"project,omitempty"`
	cancel   context.CancelFunc
}

func main() {
	root, err := os.Getwd()
	if err != nil {
		log.Fatal(err)
	}
	if exe, err := os.Executable(); err == nil {
		for _, candidate := range []string{filepath.Dir(exe), filepath.Dir(filepath.Dir(exe))} {
			if isWorkspace(candidate) {
				root = candidate
				break
			}
		}
		if isWorkspace(root) {
			for _, candidate := range []string{filepath.Dir(exe), filepath.Dir(filepath.Dir(exe))} {
				if isWorkspace(candidate) && len(candidate) < len(root) {
					root = candidate
				}
			}
		}
	}
	app := &App{root: root, ffmpeg: findFFmpeg(root)}
	for _, name := range []string{"incoming", "interviews", "interruptions", "models"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0755); err != nil {
			log.Fatal(err)
		}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", app.index)
	mux.HandleFunc("GET /media", app.media)
	mux.HandleFunc("GET /api/files", app.files)
	mux.HandleFunc("GET /api/interruption-files", app.interruptionFiles)
	mux.HandleFunc("POST /api/transcribe", app.transcribe)
	mux.HandleFunc("GET /api/transcribe/status", app.transcribeStatus)
	mux.HandleFunc("POST /api/transcribe/cancel", app.cancelTranscription)
	mux.HandleFunc("POST /api/project", app.saveProject)
	mux.HandleFunc("POST /api/assemble", app.assemble)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		log.Fatal(err)
	}
	url := "http://" + listener.Addr().String()
	log.Printf("InterviewKit открыт: %s", url)
	go openBrowser(url)
	log.Fatal(http.Serve(listener, mux))
}

func isWorkspace(path string) bool {
	for _, name := range []string{"incoming", "interviews", "interruptions", "models"} {
		info, err := os.Stat(filepath.Join(path, name))
		if err != nil || !info.IsDir() {
			return false
		}
	}
	return true
}

func (a *App) index(w http.ResponseWriter, r *http.Request) {
	data, err := webFiles.ReadFile("web/index.html")
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(data)
}
func (a *App) media(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Query().Get("path")
	allowed := within(filepath.Join(a.root, "incoming"), path) || within(filepath.Join(a.root, "interviews"), path) || within(filepath.Join(a.root, "interruptions"), path)
	if !allowed {
		http.Error(w, "media path is outside the workspace", http.StatusForbidden)
		return
	}
	http.ServeFile(w, r, path)
}
func (a *App) files(w http.ResponseWriter, _ *http.Request) {
	entries, err := os.ReadDir(filepath.Join(a.root, "incoming"))
	if err != nil {
		fail(w, err, 500)
		return
	}
	var result []map[string]string
	for _, e := range entries {
		if !e.IsDir() && isVideo(e.Name()) {
			result = append(result, map[string]string{"name": e.Name(), "path": filepath.Join(a.root, "incoming", e.Name())})
		}
	}
	jsonOut(w, result)
}
func (a *App) interruptionFiles(w http.ResponseWriter, _ *http.Request) {
	var result []map[string]string
	err := filepath.WalkDir(filepath.Join(a.root, "interruptions"), func(path string, e os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !e.IsDir() && isVideo(e.Name()) {
			result = append(result, map[string]string{"name": e.Name(), "path": path})
		}
		return nil
	})
	if err != nil {
		fail(w, err, 500)
		return
	}
	sort.Slice(result, func(i, j int) bool { return strings.ToLower(result[i]["name"]) < strings.ToLower(result[j]["name"]) })
	jsonOut(w, result)
}
func (a *App) transcribe(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Path string `json:"path"`
		Name string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		fail(w, err, 400)
		return
	}
	source, err := filepath.Abs(req.Path)
	if err != nil || !isVideo(source) {
		fail(w, errors.New("укажите существующий видеофайл"), 400)
		return
	}
	if _, err := os.Stat(source); err != nil {
		fail(w, err, 400)
		return
	}
	name := safeName(req.Name)
	if name == "" {
		name = safeName(strings.TrimSuffix(filepath.Base(source), filepath.Ext(source)))
	}
	if name == "" {
		name = "Гость"
	}
	base := time.Now().Format("2006-01-02") + "_" + name
	folder := filepath.Join(a.root, "interviews", base)
	for i := 1; ; i++ {
		if _, err := os.Stat(folder); errors.Is(err, os.ErrNotExist) {
			break
		}
		folder = filepath.Join(a.root, "interviews", fmt.Sprintf("%s_%d", base, i))
	}
	a.jobMu.Lock()
	if a.job != nil && a.job.Status == "running" {
		a.jobMu.Unlock()
		fail(w, errors.New("распознавание уже выполняется"), http.StatusConflict)
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	job := &TranscriptionJob{ID: fmt.Sprintf("%d", time.Now().UnixNano()), Status: "running", Stage: "Подготовка", Progress: 1, cancel: cancel}
	a.job = job
	a.jobMu.Unlock()

	go a.runTranscription(ctx, job, source, name, folder)
	jsonOut(w, map[string]string{"id": job.ID, "status": job.Status})
}

func (a *App) runTranscription(ctx context.Context, job *TranscriptionJob, source, name, folder string) {
	failJob := func(err error) {
		a.jobMu.Lock()
		defer a.jobMu.Unlock()
		if ctx.Err() != nil {
			job.Status, job.Stage = "cancelled", "Отменено"
		} else {
			job.Status, job.Stage, job.Error = "error", "Ошибка", err.Error()
		}
	}
	if err := os.MkdirAll(folder, 0755); err != nil {
		failJob(err)
		return
	}
	raw := filepath.Join(folder, "raw"+strings.ToLower(filepath.Ext(source)))
	a.setJobStage(job, "Копирование видео", 5)
	if err := copyFile(source, raw); err != nil {
		failJob(err)
		return
	}
	audio := filepath.Join(folder, "audio.wav")
	a.setJobStage(job, "Извлечение звука", 10)
	if _, err := a.run(ctx, "-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "1", "-y", "-i", raw, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio); err != nil {
		failJob(err)
		return
	}
	a.setJobStage(job, "Распознавание локальной моделью Whisper", 25)
	segments, err := a.runWhisper(ctx, audio)
	if err != nil {
		failJob(err)
		return
	}
	items := make([]Item, 0, len(segments))
	for i := range segments {
		segments[i].ID = fmt.Sprintf("s%d", i+1)
		items = append(items, Item{ID: segments[i].ID, Kind: "segment", SourcePath: raw, Start: segments[i].Start, End: segments[i].End, Text: segments[i].Text, Words: segments[i].Words})
	}
	p := Project{Name: name, Source: raw, Segments: segments, Items: items, Folder: folder}
	if err := saveProjectFile(&p); err != nil {
		failJob(err)
		return
	}
	a.jobMu.Lock()
	job.Status, job.Stage, job.Progress, job.Project = "done", "Готово", 100, &p
	a.jobMu.Unlock()
}

func (a *App) setJobStage(job *TranscriptionJob, stage string, progress int) {
	a.jobMu.Lock()
	job.Stage, job.Progress = stage, progress
	a.jobMu.Unlock()
}

func (a *App) transcribeStatus(w http.ResponseWriter, _ *http.Request) {
	a.jobMu.Lock()
	defer a.jobMu.Unlock()
	if a.job == nil {
		jsonOut(w, map[string]string{"status": "idle"})
		return
	}
	jsonOut(w, a.job)
}

func (a *App) cancelTranscription(w http.ResponseWriter, _ *http.Request) {
	a.jobMu.Lock()
	defer a.jobMu.Unlock()
	if a.job == nil || a.job.Status != "running" {
		fail(w, errors.New("нет активного распознавания"), http.StatusConflict)
		return
	}
	a.job.cancel()
	a.job.Status, a.job.Stage = "cancelled", "Отменено"
	jsonOut(w, map[string]string{"status": "cancelling"})
}

func (a *App) runWhisper(ctx context.Context, audio string) ([]Segment, error) {
	cli := filepath.Join(a.root, "bin", "whisper", "Release", "whisper-cli.exe")
	model := filepath.Join(a.root, "models", "ggml-small-q5_1.bin")
	if _, err := os.Stat(cli); err != nil {
		return nil, fmt.Errorf("не найден нативный Whisper CLI: %s", cli)
	}
	if _, err := os.Stat(model); err != nil {
		return nil, fmt.Errorf("не найдена локальная модель whisper.cpp: %s", model)
	}
	prefix := filepath.Join(filepath.Dir(audio), "whisper-result")
	resultFile := prefix + ".json"
	_ = os.Remove(resultFile)
	threads := runtime.NumCPU() - 1
	if threads < 1 {
		threads = 1
	}
	if threads > 4 {
		threads = 4
	}
	cmd := exec.CommandContext(ctx, cli,
		"-m", model,
		"-f", audio,
		"-l", "ru",
		"-t", fmt.Sprint(threads),
		"-ng", "-sow", "-ojf", "-of", prefix, "-np",
	)
	cmd.Dir = filepath.Dir(cli)
	output, err := cmd.CombinedOutput()
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		text := strings.TrimSpace(string(output))
		if len(text) > 3000 {
			text = text[len(text)-3000:]
		}
		return nil, fmt.Errorf("whisper.cpp: %w\n%s", err, text)
	}
	b, err := os.ReadFile(resultFile)
	if err != nil {
		return nil, fmt.Errorf("whisper.cpp не создал JSON: %w", err)
	}
	var result struct {
		Transcription []struct {
			Offsets struct {
				From int64 `json:"from"`
				To   int64 `json:"to"`
			} `json:"offsets"`
			Text   string `json:"text"`
			Tokens []struct {
				Text    string `json:"text"`
				Offsets struct {
					From int64 `json:"from"`
					To   int64 `json:"to"`
				} `json:"offsets"`
			} `json:"tokens"`
		} `json:"transcription"`
	}
	if err := json.Unmarshal(b, &result); err != nil {
		return nil, fmt.Errorf("не удалось прочитать JSON whisper.cpp: %w", err)
	}
	segments := make([]Segment, 0, len(result.Transcription))
	for _, item := range result.Transcription {
		text := strings.TrimSpace(item.Text)
		if text == "" || item.Offsets.To <= item.Offsets.From {
			continue
		}
		segments = append(segments, Segment{Start: float64(item.Offsets.From) / 1000, End: float64(item.Offsets.To) / 1000, Text: text, Words: wordsFromTokens(item.Tokens)})
	}
	if len(segments) == 0 {
		return nil, errors.New("локальная модель не обнаружила речь")
	}
	return segments, nil
}

type whisperToken struct {
	Text    string `json:"text"`
	Offsets struct {
		From int64 `json:"from"`
		To   int64 `json:"to"`
	} `json:"offsets"`
}

func wordsFromTokens(tokens []struct {
	Text    string `json:"text"`
	Offsets struct {
		From int64 `json:"from"`
		To   int64 `json:"to"`
	} `json:"offsets"`
}) []Word {
	var words []Word
	for _, token := range tokens {
		if token.Text == "" || strings.HasPrefix(token.Text, "[_") {
			continue
		}
		startsWord := len(strings.TrimLeft(token.Text, " \t\r\n")) != len(token.Text)
		part := strings.TrimSpace(token.Text)
		if part == "" {
			continue
		}
		start, end := float64(token.Offsets.From)/1000, float64(token.Offsets.To)/1000
		if startsWord || len(words) == 0 {
			words = append(words, Word{Text: part, Start: start, End: end})
			continue
		}
		last := &words[len(words)-1]
		last.Text += part
		if start > 0 && (last.Start <= 0 || start < last.Start) {
			last.Start = start
		}
		if end > last.End {
			last.End = end
		}
	}
	return words
}
func (a *App) saveProject(w http.ResponseWriter, r *http.Request) {
	var p Project
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		fail(w, err, 400)
		return
	}
	if !within(filepath.Join(a.root, "interviews"), p.Folder) {
		fail(w, errors.New("недопустимый путь проекта"), 400)
		return
	}
	for _, item := range p.Items {
		if item.Kind != "segment" && item.Kind != "interruption" {
			fail(w, errors.New("неизвестный тип элемента"), 400)
			return
		}
		if item.Kind == "interruption" && !within(filepath.Join(a.root, "interruptions"), item.SourcePath) {
			fail(w, errors.New("перебивка должна быть в interruptions"), 400)
			return
		}
	}
	if err := saveProjectFile(&p); err != nil {
		fail(w, err, 500)
		return
	}
	jsonOut(w, map[string]string{"status": "saved"})
}
func (a *App) assemble(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Project Project `json:"project"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		fail(w, err, 400)
		return
	}
	p := &req.Project
	repairTimeline(p)
	if !within(filepath.Join(a.root, "interviews"), p.Folder) || len(p.Items) == 0 {
		fail(w, errors.New("проект или монтажный план пуст"), 400)
		return
	}
	if err := saveProjectFile(p); err != nil {
		fail(w, err, 500)
		return
	}
	tmp := filepath.Join(p.Folder, "_render")
	if err := os.MkdirAll(tmp, 0755); err != nil {
		fail(w, err, 500)
		return
	}
	defer os.RemoveAll(tmp)
	var concat strings.Builder
	for i, item := range p.Items {
		out := filepath.Join(tmp, fmt.Sprintf("clip_%04d.mp4", i))
		baseArgs := []string{"-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "1", "-filter_threads", "1", "-y"}
		args := append([]string(nil), baseArgs...)
		var retryArgs []string
		hasSourceAudio := false
		if item.Kind == "segment" {
			if item.End <= item.Start || !within(filepath.Join(a.root, "interviews"), item.SourcePath) {
				fail(w, errors.New("неверный исходный сегмент"), 400)
				return
			}
			start := fmt.Sprintf("%.3f", item.Start)
			args = append(args, "-ss", start, "-i", item.SourcePath)
			retryArgs = append(append([]string(nil), baseArgs...), "-i", item.SourcePath, "-ss", start)
			hasSourceAudio = hasAudio(a, item.SourcePath)
			if hasSourceAudio {
				args = append(args, "-map", "0:v:0", "-map", "0:a:0")
				retryArgs = append(retryArgs, "-map", "0:v:0", "-map", "0:a:0")
			} else {
				args = append(args, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0")
				retryArgs = append(retryArgs, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0")
			}
			encodeArgs := []string{"-t", fmt.Sprintf("%.3f", item.End-item.Start), "-vf", videoFilter, "-c:v", "libx264", "-threads:v", "1", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-threads:a", "1", "-ar", "48000", "-ac", "2", "-shortest"}
			args = append(args, encodeArgs...)
			retryArgs = append(retryArgs, encodeArgs...)
		} else {
			if !within(filepath.Join(a.root, "interruptions"), item.SourcePath) {
				fail(w, errors.New("перебивка должна находиться в interruptions"), 400)
				return
			}
			args = append(args, "-i", item.SourcePath)
			if hasAudio(a, item.SourcePath) {
				args = append(args, "-map", "0:v:0", "-map", "0:a:0")
			} else {
				args = append(args, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0")
			}
			args = append(args, "-vf", videoFilter, "-c:v", "libx264", "-threads:v", "1", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-threads:a", "1", "-ar", "48000", "-ac", "2", "-shortest")
		}
		args = append(args, out)
		if retryArgs != nil {
			retryArgs = append(retryArgs, out)
		}
		if _, err := a.run(r.Context(), args...); err != nil {
			if retryArgs == nil {
				fail(w, err, 500)
				return
			}
			_ = os.Remove(out)
			if _, retryErr := a.run(r.Context(), retryArgs...); retryErr != nil {
				_ = os.Remove(out)
				if splitErr := a.renderSegmentSplit(r.Context(), item, out, hasSourceAudio); splitErr != nil {
					fail(w, fmt.Errorf("обычная и раздельная сборка фрагмента не удались: %w", splitErr), 500)
					return
				}
			}
		}
		concat.WriteString("file '")
		concat.WriteString(strings.ReplaceAll(strings.ReplaceAll(out, "\\", "/"), "'", "'\\''"))
		concat.WriteString("'\n")
	}
	list := filepath.Join(tmp, "concat.txt")
	if err := os.WriteFile(list, []byte(concat.String()), 0644); err != nil {
		fail(w, err, 500)
		return
	}
	final := filepath.Join(p.Folder, "final.mp4")
	if _, err := a.run(r.Context(), "-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "1", "-y", "-f", "concat", "-safe", "0", "-i", list, "-c", "copy", final); err != nil {
		if _, retry := a.run(r.Context(), "-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "1", "-filter_threads", "1", "-y", "-f", "concat", "-safe", "0", "-i", list, "-c:v", "libx264", "-threads:v", "1", "-c:a", "aac", "-threads:a", "1", final); retry != nil {
			fail(w, retry, 500)
			return
		}
	}
	jsonOut(w, map[string]string{"output": final})
}

func (a *App) renderSegmentSplit(ctx context.Context, item Item, out string, sourceHasAudio bool) error {
	duration := fmt.Sprintf("%.3f", item.End-item.Start)
	start := fmt.Sprintf("%.3f", item.Start)
	video := out + ".video.mp4"
	audio := out + ".audio.m4a"
	defer os.Remove(video)
	defer os.Remove(audio)
	common := []string{"-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "1", "-filter_threads", "1", "-y"}
	videoArgs := append(append([]string(nil), common...), "-i", item.SourcePath, "-ss", start, "-map", "0:v:0", "-t", duration, "-an", "-vf", videoFilter, "-c:v", "libx264", "-threads:v", "1", "-preset", "veryfast", "-crf", "20", video)
	if _, err := a.run(ctx, videoArgs...); err != nil {
		return err
	}
	var audioArgs []string
	if sourceHasAudio {
		audioArgs = append(append([]string(nil), common...), "-i", item.SourcePath, "-ss", start, "-map", "0:a:0", "-t", duration, "-vn", "-c:a", "aac", "-threads:a", "1", "-ar", "48000", "-ac", "2", audio)
	} else {
		audioArgs = append(append([]string(nil), common...), "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-t", duration, "-c:a", "aac", "-threads:a", "1", audio)
	}
	if _, err := a.run(ctx, audioArgs...); err != nil {
		return err
	}
	muxArgs := []string{"-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "1", "-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", out}
	_, err := a.run(ctx, muxArgs...)
	return err
}

func repairTimeline(p *Project) {
	for i := range p.Items {
		item := &p.Items[i]
		if item.Kind != "segment" || len(item.Words) == 0 {
			continue
		}
		if item.End <= item.Start {
			item.End = item.Words[len(item.Words)-1].End
		}
	}
}

const videoFilter = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"

func (a *App) run(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, a.ffmpeg, args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		text := string(output)
		if len(text) > 2500 {
			text = text[len(text)-2500:]
		}
		return output, fmt.Errorf("FFmpeg (%s %s): %w\n%s", a.ffmpeg, strings.Join(args, " "), err, text)
	}
	return output, nil
}
func hasAudio(a *App, file string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	output, _ := a.run(ctx, "-hide_banner", "-i", file)
	return strings.Contains(string(output), "Audio:")
}
func saveProjectFile(p *Project) error {
	b, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(p.Folder, "project.json"), b, 0644)
}
func findFFmpeg(root string) string {
	for _, p := range []string{filepath.Join(root, "bin", "ffmpeg.exe"), filepath.Join(root, "ffmpeg.exe")} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		return p
	}
	return "ffmpeg"
}
func openBrowser(url string) {
	if runtime.GOOS == "windows" {
		_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	} else if runtime.GOOS == "darwin" {
		_ = exec.Command("open", url).Start()
	} else {
		_ = exec.Command("xdg-open", url).Start()
	}
}
func jsonOut(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(v)
}
func fail(w http.ResponseWriter, err error, status int) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}
func safeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r >= 'А' && r <= 'я' || r == 'Ё' || r == 'ё' || r == '-' || r == '_' || r == ' ' {
			b.WriteRune(r)
		}
	}
	return strings.TrimSpace(b.String())
}
func isVideo(name string) bool {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v":
		return true
	}
	return false
}
func within(base, target string) bool {
	b, e1 := filepath.Abs(base)
	t, e2 := filepath.Abs(target)
	if e1 != nil || e2 != nil {
		return false
	}
	rel, err := filepath.Rel(b, t)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}
