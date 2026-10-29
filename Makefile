# Builds the analyzer (universal: Apple silicon and Intel).
CXX ?= clang++
CXXFLAGS = -std=c++17 -O2 -Wall -Wextra -arch arm64 -arch x86_64 -mmacosx-version-min=13.0

build/mic_switch_analyze: analyzer/mic_switch_analyze.cpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $<

test: build/mic_switch_analyze
	python3 -I tests/test_analyzer.py build/mic_switch_analyze

clean:
	rm -rf build

.PHONY: test clean
