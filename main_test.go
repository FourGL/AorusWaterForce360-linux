package main

import (
	"bytes"
	"reflect"
	"testing"
)

func head(r []byte, n int) []byte { return r[:n] }

func TestCurvePayloadMatchesDriverTemplate(t *testing.T) {
	// set_rpm_speed_cmd_template from waterforce-hwmon with be16(1500)=05 DC at offsets 5, 8, 11, 14
	want := []byte{0x99, 0xE6, 0x01, 0x01, 0, 0x05, 0xDC, 0x1E, 0x05, 0xDC, 0x32, 0x05, 0xDC, 0x41, 0x05, 0xDC}
	got := buildCurvePayload(0x01, 0x01, flatCurve(1500))
	if !bytes.Equal(got[:16], want) || len(got) != 6144 || bytes.Count(got[16:], []byte{0}) != 6144-16 {
		t.Fatalf("got % X", got[:20])
	}
}

func TestCurvePayloadPoints(t *testing.T) {
	// the probe's curve: 0/40/60/80 °C -> 800/1400/2000/2600 RPM, as read back with D9 01
	curve := []curvePoint{{0, 800}, {40, 1400}, {60, 2000}, {80, 2600}}
	want := []byte{0x99, 0xE6, 0x01, 0x01, 0x00, 0x03, 0x20, 0x28, 0x05, 0x78, 0x3C, 0x07, 0xD0, 0x50, 0x0A, 0x28}
	if got := buildCurvePayload(0x01, 0x01, curve); !bytes.Equal(got[:16], want) {
		t.Fatalf("got % X", got[:16])
	}
}

func TestCoolingPayloads(t *testing.T) {
	if n := len(buildCoolingPayloads(cooling{}, cooling{})); n != 0 {
		t.Fatalf("untouched must send nothing, got %d", n)
	}
	r := buildCoolingPayloads(cooling{mode: "fixed", rpm: 1200}, cooling{mode: "fixed", rpm: 2700})
	if len(r) != 4 ||
		!bytes.Equal(head(r[0], 4), []byte{0x99, 0xE5, 0x01, 0x01}) ||
		!bytes.Equal(head(r[1], 7), []byte{0x99, 0xE6, 0x01, 0x01, 0, 0x04, 0xB0}) ||
		!bytes.Equal(head(r[2], 4), []byte{0x99, 0xE5, 0x02, 0x01}) ||
		!bytes.Equal(head(r[3], 7), []byte{0x99, 0xE6, 0x04, 0x02, 0, 0x0A, 0x8C}) {
		t.Fatal("fixed fan + pump")
	}
	r = buildCoolingPayloads(cooling{mode: "factory"}, cooling{mode: "factory"})
	if len(r) != 2 || !bytes.Equal(head(r[0], 4), []byte{0x99, 0xE5, 0x01, 0x05}) || !bytes.Equal(head(r[1], 4), []byte{0x99, 0xE5, 0x02, 0x00}) {
		t.Fatal("factory profiles")
	}
	curve := []curvePoint{{30, 1800}, {50, 2200}, {70, 2800}, {85, 3200}}
	r = buildCoolingPayloads(cooling{}, cooling{mode: "curve", curve: curve})
	if len(r) != 2 || !bytes.Equal(head(r[0], 4), []byte{0x99, 0xE5, 0x02, 0x01}) ||
		!bytes.Equal(head(r[1], 16), []byte{0x99, 0xE6, 0x04, 0x02, 30, 0x07, 0x08, 50, 0x08, 0x98, 70, 0x0A, 0xF0, 85, 0x0C, 0x80}) {
		t.Fatal("pump curve")
	}
}

func TestEnvValidation(t *testing.T) {
	curve := []curvePoint{{30, 800}, {50, 1200}, {70, 2000}, {85, 2750}}
	for _, c := range []struct {
		mode, rpm, curve string
		want             cooling
	}{
		{"", "", "", cooling{}},
		{"", "0", "", cooling{mode: "factory"}}, // older drop-ins: RPM only
		{"", "1200", "", cooling{mode: "fixed", rpm: 1200}},
		{"", "700", "", cooling{}},
		{"factory", "1200", "", cooling{mode: "factory"}},
		{"fixed", "1200", "30:800,50:1200,70:2000,85:2750", cooling{mode: "fixed", rpm: 1200}},
		{"fixed", "2800", "", cooling{}},
		{"curve", "1200", "30:800,50:1200,70:2000,85:2750", cooling{mode: "curve", curve: curve}},
		{"curve", "1200", "30:800,50:1200,70:2000", cooling{}},            // 3 points
		{"curve", "1200", "30:800,50:1200,50:2000,85:2750", cooling{}},    // temps not rising
		{"curve", "1200", "30:800,50:1200,70:2000,101:2750", cooling{}},   // over 100 °C
		{"curve", "1200", "30:700,50:1200,70:2000,85:2750", cooling{}},    // below min RPM
		{"curve", "1200", "30:800,50:1200,70:2000,85:2750;rm", cooling{}}, // junk
		{"turbo", "1200", "", cooling{}},
	} {
		t.Setenv("AWF_FAN_MODE", c.mode)
		t.Setenv("AWF_FAN_RPM", c.rpm)
		t.Setenv("AWF_FAN_CURVE", c.curve)
		loadEnvConfig()
		if !reflect.DeepEqual(fan, c.want) {
			t.Errorf("%q/%q/%q: got %+v, want %+v", c.mode, c.rpm, c.curve, fan, c.want)
		}
	}
}
