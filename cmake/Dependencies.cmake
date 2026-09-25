# Third-party dependencies, pinned. Downloads go through CMake FetchContent.
include(FetchContent)

# Behind TLS-intercepting proxies (corporate networks, CI sandboxes) use the configured CA bundle.
if(NOT CMAKE_TLS_CAINFO AND DEFINED ENV{SSL_CERT_FILE})
  set(CMAKE_TLS_CAINFO "$ENV{SSL_CERT_FILE}")
endif()

# ---------------------------------------------------------------------------- DuckDB (prebuilt C API)
# Set SORA_DUCKDB_ROOT to a directory with duckdb.h and the library to use a local copy instead.
set(SORA_DUCKDB_VERSION "1.5.5")
set(SORA_DUCKDB_ROOT "" CACHE PATH "Directory containing duckdb.h and libduckdb (skips download)")
if(NOT SORA_DUCKDB_ROOT)
  if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64")
    set(_duckdb_asset "libduckdb-linux-amd64.zip")
    set(_duckdb_sha256 "1fb8ce388157d84a25abe685a8a2520bf00c00321821968e4bb398fd766e7abb")
  elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
    set(_duckdb_asset "libduckdb-linux-arm64.zip")
  elseif(APPLE)
    set(_duckdb_asset "libduckdb-osx-universal.zip")
  elseif(WIN32)
    set(_duckdb_asset "libduckdb-windows-amd64.zip")
  else()
    message(FATAL_ERROR "No prebuilt DuckDB for this platform; set SORA_DUCKDB_ROOT")
  endif()
  set(_duckdb_url "https://github.com/duckdb/duckdb/releases/download/v${SORA_DUCKDB_VERSION}/${_duckdb_asset}")
  if(_duckdb_sha256)
    FetchContent_Declare(duckdb_bin URL ${_duckdb_url} URL_HASH SHA256=${_duckdb_sha256} DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
  else()
    message(WARNING "DuckDB ${_duckdb_asset}: no pinned SHA-256 for this platform yet")
    FetchContent_Declare(duckdb_bin URL ${_duckdb_url} DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
  endif()
  FetchContent_MakeAvailable(duckdb_bin)
  set(SORA_DUCKDB_ROOT "${duckdb_bin_SOURCE_DIR}")
endif()

find_library(SORA_DUCKDB_LIBRARY NAMES duckdb PATHS "${SORA_DUCKDB_ROOT}" NO_DEFAULT_PATH REQUIRED)
add_library(sora::duckdb SHARED IMPORTED GLOBAL)
set_target_properties(sora::duckdb PROPERTIES
  IMPORTED_LOCATION "${SORA_DUCKDB_LIBRARY}"
  IMPORTED_IMPLIB "${SORA_DUCKDB_LIBRARY}"
  INTERFACE_INCLUDE_DIRECTORIES "${SORA_DUCKDB_ROOT}")
# Find libduckdb next to the binary when installed, and in the fetched directory when run from the build tree.
set(CMAKE_BUILD_RPATH "${SORA_DUCKDB_ROOT}")
set(CMAKE_INSTALL_RPATH "$ORIGIN;$ORIGIN/../lib")

# ---------------------------------------------------------------------------- rapidyaml (scenario files)
# Single-header release asset. Set SORA_RYML_HEADER to a local copy to build offline.
set(SORA_RYML_HEADER "" CACHE FILEPATH "Path to rapidyaml single header (skips download)")
if(NOT SORA_RYML_HEADER)
  FetchContent_Declare(ryml_hdr
    URL https://github.com/biojppm/rapidyaml/releases/download/v0.9.0/rapidyaml-0.9.0.hpp
    URL_HASH SHA256=07912e0a8b7da287c143b374e03879caec2dd4f8f011f5e8834aaf558d323ab8
    DOWNLOAD_NO_EXTRACT TRUE)
  FetchContent_MakeAvailable(ryml_hdr)
  set(SORA_RYML_HEADER "${ryml_hdr_SOURCE_DIR}/rapidyaml-0.9.0.hpp")
endif()
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/ryml")
file(COPY_FILE "${SORA_RYML_HEADER}" "${CMAKE_BINARY_DIR}/ryml/ryml_all.hpp" ONLY_IF_DIFFERENT)
file(WRITE "${CMAKE_BINARY_DIR}/ryml/ryml_impl.cpp" "#define RYML_SINGLE_HDR_DEFINE_NOW\n#include \"ryml_all.hpp\"\n")
add_library(sora_ryml STATIC "${CMAKE_BINARY_DIR}/ryml/ryml_impl.cpp")
target_include_directories(sora_ryml SYSTEM PUBLIC "${CMAKE_BINARY_DIR}/ryml")
set_target_properties(sora_ryml PROPERTIES POSITION_INDEPENDENT_CODE ON)
add_library(sora::ryml ALIAS sora_ryml)

# ---------------------------------------------------------------------------- doctest (unit tests)
# Vendored single header: third_party/doctest/doctest.h
