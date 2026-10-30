package main

import (
	"context"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSafeName(t *testing.T) {
	if got := safeName("Иван и Мария / интервью"); got != "Иван и Мария  интервью" {
		t.Fatalf("safeName() = %q", got)
	}
}

func TestVideoExtensions(t *testing.T) {
	for _, name := range []string{"guest.mp4", "clip.MOV", "recording.webm"} {
		if !isVideo(name) {
			t.Errorf("isVideo(%q) = false", name)
		}
	}
	for _, name := range []string{"audio.wav", "notes.txt", "folder"} {
		if isVideo(name) {
			t.Errorf("isVideo(%q) = true", name)
		}
	}
}

func TestWithinRejectsPathTraversal(t *testing.T) {
	if !within(`C:\project\interviews`, `C:\project\interviews\one\raw.mp4`) {
		t.Fatal("expected a project file to be inside interviews")
	}
	if within(`C:\project\interviews`, `C:\project\interruptions\clip.mp4`) {
		t.Fatal("accepted a path outside interviews")
	}
}

func TestRunWhisperReadsNativeJSON(t *testing.T) {
	if os.Getenv("INTERVIEWKIT_STT_SMOKE") == "" {
		t.Skip("set INTERVIEWKIT_STT_SMOKE to a 16 kHz WAV for native STT smoke test")
	}
	root, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	a := &App{root: root}
	segments, err := a.runWhisper(context.Background(), filepath.Clean(os.Getenv("INTERVIEWKIT_STT_SMOKE")))
	if err != nil {
		t.Fatal(err)
	}
	if len(segments) == 0 || segments[0].End <= segments[0].Start {
		t.Fatalf("invalid segments: %#v", segments)
	}
	wordCount := 0
	for _, segment := range segments {
		wordCount += len(segment.Words)
	}
	if wordCount == 0 {
		t.Fatal("native STT returned no word timestamps")
	}
}

func TestRepairTimelineDoesNotRestoreDeletedContinuation(t *testing.T) {
	p := Project{
		Segments: []Segment{{Start: 0, End: 3, Words: []Word{{Text: "один", Start: 0.1, End: 1}, {Text: "два", Start: 1, End: 2}, {Text: "три", Start: 2, End: 3}}}},
		Items: []Item{
			{ID: "left", Kind: "segment", SourcePath: `C:\video.mp4`, Start: 0, End: 0, Words: []Word{{Text: "один", Start: 0.1, End: 1}, {Text: "два", Start: 1, End: 2}}},
			{ID: "b", Kind: "interruption", SourcePath: `C:\broll.mp4`},
		},
	}
	repairTimeline(&p)
	if len(p.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(p.Items))
	}
	if p.Items[0].End != 2 {
		t.Fatalf("left end = %v, want 2", p.Items[0].End)
	}
	if p.Items[1].Kind != "interruption" {
		t.Fatalf("deleted continuation was restored: %#v", p.Items)
	}
}

func TestIndexIsServedAsHTML(t *testing.T) {
	recorder := httptest.NewRecorder()
	(&App{}).index(recorder, httptest.NewRequest("GET", "/", nil))
	if got := recorder.Header().Get("Content-Type"); got != "text/html; charset=utf-8" {
		t.Fatalf("Content-Type = %q", got)
	}
	if !strings.Contains(recorder.Body.String(), "<!doctype html>") {
		t.Fatal("response does not contain HTML document")
	}
}
