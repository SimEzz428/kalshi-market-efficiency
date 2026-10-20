CXX      ?= g++
CXXSTD   := -std=c++17
WARN     := -Wall -Wextra -Wpedantic
THREAD   := -pthread
OPT      := -O2 -g
CXXFLAGS := $(CXXSTD) $(WARN) $(OPT) $(THREAD)

UNAME := $(shell uname -s)

ifeq ($(UNAME),Darwin)
BREW        := $(shell brew --prefix 2>/dev/null || echo /opt/homebrew)
OPENSSL_DIR := $(shell brew --prefix openssl@3 2>/dev/null || echo $(BREW)/opt/openssl@3)
export PKG_CONFIG_PATH := $(OPENSSL_DIR)/lib/pkgconfig:$(BREW)/lib/pkgconfig:$(PKG_CONFIG_PATH)
EXTRA_INC   := -I$(BREW)/include
EXTRA_LIB   := -L$(BREW)/lib
else
EXTRA_INC   :=
EXTRA_LIB   :=
endif

PKGS     := libcurl openssl sqlite3
DEP_CF   := $(shell pkg-config --cflags $(PKGS) 2>/dev/null)
DEP_LD   := $(shell pkg-config --libs   $(PKGS) 2>/dev/null)

ifeq ($(strip $(DEP_LD)),)
DEP_LD := -lcurl -lssl -lcrypto -lsqlite3
endif

CORE_SRC := book.cpp
TEST_SRC := test_book.cpp
REC_SRC  := recorder.cpp

.PHONY: all test san tsan recorder check-deps clean

all: test recorder

test: $(CORE_SRC) $(TEST_SRC) book.hpp
	$(CXX) $(CXXFLAGS) -o test_book $(CORE_SRC) $(TEST_SRC)
	./test_book

san: $(CORE_SRC) $(TEST_SRC) book.hpp
	$(CXX) $(CXXSTD) $(WARN) $(THREAD) -g -O0 -fsanitize=address,undefined \
	  -o test_book_san $(CORE_SRC) $(TEST_SRC)
	./test_book_san

tsan: $(CORE_SRC) $(TEST_SRC) book.hpp
	$(CXX) $(CXXSTD) $(WARN) $(THREAD) -g -O1 -fsanitize=thread \
	  -o test_book_tsan $(CORE_SRC) $(TEST_SRC)
	./test_book_tsan

recorder: $(CORE_SRC) $(REC_SRC) book.hpp
	$(CXX) $(CXXFLAGS) $(EXTRA_INC) $(DEP_CF) -o recorder \
	  $(CORE_SRC) $(REC_SRC) $(EXTRA_LIB) $(DEP_LD)

check-deps:
	@for p in $(PKGS); do \
	  if pkg-config --exists $$p; then \
	    printf '  %-10s %s\n' $$p "$$(pkg-config --modversion $$p)"; \
	  else \
	    printf '  %-10s MISSING\n' $$p; \
	  fi; \
	done
	@printf '  %-10s ' nlohmann/json; \
	  printf '#include <nlohmann/json.hpp>\nint main(){return 0;}\n' > .dep_probe.cpp; \
	  if $(CXX) $(CXXSTD) $(EXTRA_INC) -fsyntax-only .dep_probe.cpp 2>/dev/null; \
	    then echo found; else echo MISSING; fi; \
	  rm -f .dep_probe.cpp

clean:
	rm -f test_book test_book_san test_book_tsan recorder .dep_probe.cpp
	rm -rf *.dSYM
