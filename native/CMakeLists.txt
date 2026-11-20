cmake_minimum_required(VERSION 3.21)
project(box3d_blender_bridge VERSION 0.14.135 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)

if(MSVC)
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

include(FetchContent)
set(BOX3D_SAMPLES OFF CACHE BOOL "" FORCE)
set(BOX3D_UNIT_TESTS OFF CACHE BOOL "" FORCE)
set(BOX3D_BENCHMARKS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  box3d
  GIT_REPOSITORY https://github.com/erincatto/box3d.git
  GIT_TAG f36d061ba26998c87cb7dc593a4065aaa03fb7eb
  GIT_SHALLOW FALSE
)
FetchContent_MakeAvailable(box3d)

add_library(box3d_blender SHARED src/box3d_bridge.cpp)
target_link_libraries(box3d_blender PRIVATE box3d::box3d)

if(MSVC)
  target_compile_options(box3d_blender PRIVATE /O2 /Ob3 /GL /EHsc /bigobj)
  target_link_options(box3d_blender PRIVATE /LTCG)
endif()

set_target_properties(box3d_blender PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bin"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/bin"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bin"
  OUTPUT_NAME "box3d_blender"
)

option(TUMBLIFY_ENGINE_TESTS "Build upstream engine tests" ON)
if(TUMBLIFY_ENGINE_TESTS)

    add_subdirectory("${box3d_SOURCE_DIR}/shared" "${CMAKE_CURRENT_BINARY_DIR}/engine_shared")
    add_subdirectory("${box3d_SOURCE_DIR}/test" "${CMAKE_CURRENT_BINARY_DIR}/engine_test")
    target_include_directories(test PRIVATE "${box3d_SOURCE_DIR}/src")
endif()

