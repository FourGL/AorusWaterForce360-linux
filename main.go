/*
Aorus WaterForce 360 cooler HID updater for Linux
(°C, GHz, %CPU)

By: fourgl@gmail.com 2026

Dependencies:
  go get github.com/karalabe/hid

Build:
  1. Configure your vars of this file if needed
  2. go build -o AorusWaterForce360-linux main.go

Installation:
  1. Copy the binary to /usr/local/bin/AorusWaterForce360-linux
  2. Copy configuration file to /etc/systemd/system/AorusWaterForce360-linux.service
  3. Enable and start the service:
	 sudo systemctl daemon-reload
     sudo systemctl enable AorusWaterForce360-linux
	 sudo systemctl start AorusWaterForce360-linux
  4. Check status:
	 systemctl status AorusWaterForce360-linux

Device:
  ID 1044:7a4d Chu Yuen Enterprise Co., Ltd Castor3

Tested on:
  AMD 7700X with Arch Linux (BTW :))

Have fun!
*/

package main

import (
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	hid "github.com/karalabe/hid"
)

var debugMode = false
var refreshInterval = 5 * time.Second
var sensorLabel = "Tccd1" // Tccd1 OR Tctl OR "" for first located (Tccd1 is more precise, may be Tccd2 makes sense for some systems?)
var sensorPath = ""       // to be detected

var supportedDevices = []struct{ vid, pid uint16 }{
	{0x1044, 0x7A4D},
}

func readFirstLine(path string) string {
	b, _ := os.ReadFile(path)
	s := strings.TrimSpace(string(b))
	if s == "" {
		return ""
	}
	return s
}

func getCpuFrequency() int {
	b, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return 0
	}
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "cpu MHz") {
			parts := strings.SplitN(l, ":", 2)
			if len(parts) == 2 {
				f, _ := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
				return int(f)
			}
		}
	}
	return 0
}

func getSensorPath(sensorLabel string) string {
	dirs, _ := os.ReadDir("/sys/class/hwmon/")
	for _, d := range dirs {
		p := "/sys/class/hwmon/" + d.Name() + "/name"
		name := readFirstLine(p)
		if strings.Contains(name, "k10temp") || strings.Contains(name, "coretemp") {
			files, _ := os.ReadDir("/sys/class/hwmon/" + d.Name())
			for _, f := range files {
				if strings.HasPrefix(f.Name(), "temp") && strings.HasSuffix(f.Name(), "_label") {
					v := readFirstLine("/sys/class/hwmon/" + d.Name() + "/" + f.Name())
					if v == sensorLabel || sensorLabel == "" {
						if debugMode {
							fmt.Printf("Found temperature sensor %s at %s\n", v, d.Name())
						}
						return "/sys/class/hwmon/" + d.Name() + "/" + strings.Replace(f.Name(), "_label", "_input", 1)
					}
				}
			}
		}
	}
	zones, err := os.ReadDir("/sys/class/thermal/")
	if err != nil {
		return ""
	}

	for _, z := range zones {

		if strings.HasPrefix(z.Name(), "thermal_zone") {

			base := "/sys/class/thermal/" + z.Name()

			t := readFirstLine(base + "/type")

			if t == "x86_pkg_temp" {

				if debugMode {
					fmt.Printf("Found CPU thermal sensor: %s\n", base+"/temp")
				}

				return base + "/temp"
			}
		}
	}
	return ""
}

func getCpuTemperature(sensorPath string) int {
	if sensorPath == "" {
		return 0
	}
	v := readFirstLine(sensorPath)
	if v != "" {
		i, _ := strconv.Atoi(strings.TrimSpace(v))
		return i / 1000
	}
	return 0
}

func getCpuUsage() int {
	idle1, total1 := cpuTimes()
	time.Sleep(500 * time.Millisecond)
	idle2, total2 := cpuTimes()
	idle := idle2 - idle1
	total := total2 - total1
	if total == 0 {
		return 0
	}
	usage := 100 * (1.0 - float64(idle)/float64(total))
	return int(usage)
}

func cpuTimes() (idle, total uint64) {
	b, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, 1
	}
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "cpu ") {
			parts := strings.Fields(l)
			var vals []uint64
			for i := 1; i < len(parts); i++ {
				v, _ := strconv.ParseUint(parts[i], 10, 64)
				vals = append(vals, v)
			}
			if len(vals) > 3 {
				idle = vals[3]
				for _, v := range vals {
					total += v
				}
			}
			break
		}
	}
	return
}

func buildPayload(cTemp, cFreq, cUsage int) []byte {
	buf := make([]byte, 6144)
	buf[0] = 153
	buf[1] = 224
	//buf[2] = 0
	buf[3] = byte(cTemp)
	buf[4] = 16
	buf[5] = byte(cFreq / 1000)
	buf[6] = byte((cFreq / 100) % 10)
	buf[7] = 8
	buf[8] = 24
	//buf[9] = 0
	buf[10] = byte(cUsage)
	//buf[11] = 0
	//buf[12] = 0
	return buf
}

func main() {

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	for _, d := range supportedDevices {
		devs, _ := hid.Enumerate(d.vid, d.pid)
		if len(devs) == 0 {
			if debugMode {
				fmt.Printf("No HID device %04x:%04x\n", d.vid, d.pid)
			}
			continue
		}
		if debugMode {
			fmt.Printf("Connecting to HID device %04x:%04x\n", d.vid, d.pid)
		}
		info := devs[0]
		device, err := info.Open()
		if err != nil {
			if debugMode {
				fmt.Printf("Open HID device %04x:%04x failed: %s\n", d.vid, d.pid, err)
			}
			continue
		} else {
			if debugMode {
				fmt.Printf("HID device %04x:%04x opened\n", d.vid, d.pid)
			}
		}

		defer device.Close()

		sensorPath := getSensorPath(sensorLabel)

		for {
			select {
			case <-sig:
				if debugMode {
					fmt.Println("Shutting down")
				}
				return
			default:
				cTemp := getCpuTemperature(sensorPath)
				cFreq := getCpuFrequency()
				cUsage := getCpuUsage()
				buf := buildPayload(cTemp, cFreq, cUsage)
				_, err := device.Write(buf)
				if err != nil && debugMode {
					fmt.Println("Write to HID device failed:", err)
				}
				if debugMode {
					fmt.Printf("%s %d°C %d.%dGHz %d%%\n", ">>", cTemp, cFreq/1000, (cFreq/100)%10, cUsage)
				}
				time.Sleep(refreshInterval)
			}
		}
	}
}
