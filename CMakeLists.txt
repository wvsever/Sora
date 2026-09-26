cmake_minimum_required(VERSION 3.24)
project(sora VERSION 0.1.0 DESCRIPTION "Sora stress and scenario engine" LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

option(SORA_BUILD_TESTS "Build unit tests" ON)
option(SORA_SANITIZE "Build with address and undefined-behaviour sanitizers" OFF)

if(WIN32)
  # windows.h (pulled in by cpp-httplib and psapi) must not define min/max macros over std::min/std::max.
  add_compile_definitions(NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
endif()

include(cmake/Dependencies.cmake)

add_library(sora_core
  src/types.cpp
  src/duck.cpp
  src/dataset.cpp
  src/segmentation.cpp
  src/calibration.cpp
  src/parameters.cpp
  src/scenario.cpp
  src/projection.cpp
  src/output.cpp
  src/cr_scen.cpp
  src/cr_sector.cpp
  src/collateral.cpp
  src/off_balance.cpp
  src/validation.cpp
)
target_include_directories(sora_core PUBLIC include)
target_link_libraries(sora_core PUBLIC sora::duckdb PRIVATE sora::ryml)
target_compile_options(sora_core PRIVATE
  $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra -Wpedantic -Wconversion -Wno-sign-conversion>
  $<$<CXX_COMPILER_ID:MSVC>:/W4>)

add_executable(sora src/main.cpp)
target_link_libraries(sora PRIVATE sora_core)
if(WIN32)
  # Windows has no rpath: put duckdb.dll next to the engine (tests copy it too, see tests/cpp).
  target_link_libraries(sora PRIVATE psapi)
  add_custom_command(TARGET sora POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${SORA_DUCKDB_ROOT}/duckdb.dll" "$<TARGET_FILE_DIR:sora>")
endif()

if(SORA_SANITIZE)
  # PUBLIC: everything linking sora_core (engine, tests) is instrumented too.
  target_compile_options(sora_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(sora_core PUBLIC -fsanitize=address,undefined)
endif()

if(SORA_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests/cpp)
endif()

# Regulatory calculator REST client, REA projection and their tests.
include(cmake/Calculator.cmake)
