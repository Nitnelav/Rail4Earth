#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
BSD 3-Clause License

Copyright (c) 2022, Université Gustave-Eiffel
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
import json
import os.path
import collections
import argparse
import sys
import threading
import time
import math
import csv
import numpy as np
import types

try:
    import zmq
except ImportError as e:
    print("Please install pyzmq")
    print("pip install pyzmq")
    print("Audio capture has been disabled")
    raise e

try:
    from Crypto.Cipher import PKCS1_OAEP
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import AES
    from Crypto import Random
    from Crypto.Random import get_random_bytes
    import base64
except ImportError:
    print("Please install PyCryptodome")
    print("pip install pycryptodome")
    print("Audio encryption has been disabled")

import soundfile as sf
import io
import datetime
import tflite_runtime.interpreter as tflite
import resampy
from importlib.resources import files


class Params:
    """
      Yamnet settings
    """
    sample_rate: float = 16000.0
    stft_window_seconds: float = 0.025
    stft_hop_seconds: float = 0.010
    mel_bands: int = 64
    mel_min_hz: float = 125.0
    mel_max_hz: float = 7500.0
    log_offset: float = 0.001
    patch_window_seconds: float = 0.96
    patch_hop_seconds: float = 0.48


class Tensors:
    cores_output_index = None
    embeddings_output_index = None
    spectrogram_output_index = None
    waveform_input_index = None
    scores_output_index = None


def encrypt(audio_data, ssh_file):
    """
    Encrypt the provided string using the provided ssh public key
    :param audio_data: Audio data in bytes
    :param ssh_file: Path of the public key
    :return: Encrypted data
    """
    output_encrypted = io.BytesIO()

    # Open public key to encrypt AES key
    key = RSA.importKey(open(ssh_file).read())
    cipher = PKCS1_OAEP.new(key)

    aes_key = get_random_bytes(AES.block_size)
    iv = Random.new().read(AES.block_size)

    # Write OAEP header
    output_encrypted.write(cipher.encrypt(aes_key + iv))

    aes_cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    # pad audio data
    if len(audio_data) % AES.block_size > 0:
        audio_data = audio_data.ljust(len(audio_data) + AES.block_size - len(audio_data) % AES.block_size, b'\0')
    # Write AES data
    output_encrypted.write(aes_cipher.encrypt(audio_data))
    return output_encrypted.getvalue()


class StatusThread(threading.Thread):
    def __init__(self, trigger_processor, config):
        threading.Thread.__init__(self)
        self.trigger_processor = trigger_processor
        self.config = config

    def run(self):
        while self.config.running:
            record_time = str(datetime.timedelta(seconds=
                                                 round(self.trigger_processor.total_read / self.config.sample_rate)))
            print("%s samples read: %ld (%s)" % (datetime.datetime.now().replace(microsecond=0).isoformat(),
                                                 self.trigger_processor.total_read, record_time))
            time.sleep(self.config.delay_print_samples)


def read_yamnet_class_and_threshold(class_map_csv):
    with open(class_map_csv) as csv_file:
        reader = csv.reader(csv_file)
        next(reader)  # Skip header
        names, threshold = zip(*[[display_name.strip(), float(threshold)] for _, _, display_name, threshold in reader])
        return names, np.array(threshold, dtype=float)


def butter_highpass(cutoff, fs, order=4):
    from scipy import signal
    return signal.butter(order, cutoff / (fs / 2.0), btype='high',
                         output='sos')


class TriggerProcessor:
    """
    Service listening to zero_record and trigger sound recording according to pre-defined noise events
    """

    def __init__(self, config):
        self.config = config
        self.total_read = 0  # Total audio samples read
        self.sample_rate = self.config.sample_rate
        format_byte_width = {"S16_LE": 2, "S32_LE": 4, "FLOAT_LE": 4, "S24_3LE": 3, "S24_LE": 4}
        sample_length = format_byte_width[self.config.sample_format]
        self.bytes_per_seconds = self.sample_rate * sample_length
        self.remaining_samples = 0
        self.remaining_triggers = self.config.trigger_count
        self.last_fetch_trigger_info = 0
        self.epoch = datetime.datetime.utcfromtimestamp(0)
        # Cache samples for configured length before trigger
        self.samples_stack = collections.deque()
        self.socket = None
        self.socket_out = None
        self.yamnet_config = Params()
        tflite_path = self.config.yamnet_weights
        self.yamnet_interpreter = tflite.Interpreter(model_path=tflite_path)
        self.tensors = Tensors()

        input_details = self.yamnet_interpreter.get_input_details()
        self.tensors.waveform_input_index = input_details[0]['index']
        output_details = self.yamnet_interpreter.get_output_details()
        self.tensors.scores_output_index = output_details[0]['index']
        self.tensors.embeddings_output_index = output_details[1]['index']
        self.tensors.spectrogram_output_index = output_details[2]['index']
        self.yamnet_samples = np.zeros((int(config.yamnet_window_time *
                                 self.yamnet_config.sample_rate)),
                                       dtype=np.float32)
        # where to place new samples
        self.yamnet_samples_index = 0
        yamnet_class_map = self.config.yamnet_class_map
        if yamnet_class_map is None:
            yamnet_class_map = files('yamnet').joinpath('yamnet_class_threshold_map.csv')
        self.yamnet_classes = read_yamnet_class_and_threshold(yamnet_class_map)
        if self.config.yamnet_cutoff_frequency > 0:
            self.sos = butter_highpass(self.config.yamnet_cutoff_frequency,
                                            self.yamnet_config.sample_rate)
        else:
            self.sos = None

    def butter_highpass_filter(self, waveform):
        from scipy import signal
        return signal.sosfilt(self.sos, waveform)

    def init_socket(self):
        context = zmq.Context()
        self.socket = context.socket(zmq.SUB)
        self.socket.connect(self.config.input_address)
        self.socket.subscribe("")
        self.socket_out = context.socket(zmq.PUB)
        self.socket_out.bind(self.config.output_address)

    def process_tags(self):
        # check for sound recognition tags
        # filter and normalize signal
        if self.config.yamnet_cutoff_frequency > 0:
            waveform = self.butter_highpass_filter(self.yamnet_samples)
        if self.config.yamnet_max_gain > 0:
            # apply gain
            max_value = max(1e-12, float(np.max(np.abs(self.yamnet_samples))))
            gain = 10 * math.log10(1 / max_value)
            gain = min(self.config.yamnet_max_gain, gain)
            self.yamnet_samples *= 10 ** (gain / 10.0)
        # Predict YAMNet classes.
        self.yamnet_interpreter.resize_tensor_input(
            self.tensors.waveform_input_index,
            [len(self.yamnet_samples)], strict=True)
        self.yamnet_interpreter.allocate_tensors()
        self.yamnet_interpreter.set_tensor(self.tensors.waveform_input_index,
                                           self.yamnet_samples)
        self.yamnet_interpreter.invoke()
        scores, embeddings, spectrogram = (
            self.yamnet_interpreter.get_tensor(self.tensors.scores_output_index),
            self.yamnet_interpreter.get_tensor(self.tensors.embeddings_output_index),
            self.yamnet_interpreter.get_tensor(self.tensors.spectrogram_output_index))
        return scores, embeddings, spectrogram

    def fetch_audio_data(self, feed_cache=True):
        if feed_cache:
            if self.samples_stack is None:
                self.samples_stack = collections.deque()
        audio_data_bytes = np.frombuffer(self.socket.recv(), dtype=np.single)
        self.total_read += len(audio_data_bytes)
        if feed_cache:
            self.samples_stack.append(audio_data_bytes)
            # will keep keep_only_samples samples, and drop older stack elements
            keep_only_samples = max(self.config.cached_length, self.yamnet_config.patch_window_seconds) * \
                                self.config.sample_rate
            while sum([len(s) for s in self.samples_stack]) > keep_only_samples + len(audio_data_bytes):
                self.samples_stack.popleft()
        return audio_data_bytes

    def run(self):
        reference_pressure = 1 / 10 ** (
                    (94 - self.config.sensitivity) / 20.0)
        status = "wait_trigger"
        last_day_of_year = datetime.datetime.now().timetuple().tm_yday
        self.init_socket()
        document = {}
        processing_time = 0
        while True:
            cur_time = time.time()
            if last_day_of_year != datetime.datetime.now().timetuple().tm_yday\
                    and "trigger_count" in self.config:
                # reset trigger counter each day
                print("Reset trigger counter")
                last_day_of_year = datetime.datetime.now().timetuple().tm_yday
                self.remaining_triggers = self.config.trigger_count
            if self.config is not None and status == "wait_trigger":
                waveform = self.fetch_audio_data()
                deb = time.time()
                if self.config.sample_rate != self.yamnet_config.sample_rate:
                    # resample if necessary
                    waveform = resampy.resample(waveform,
                                                self.config.sample_rate,
                                                self.yamnet_config.sample_rate,
                                                filter=self.config.
                                                resample_method)
                len_to_extract = min(len(waveform), len(self.yamnet_samples) -
                                     self.yamnet_samples_index)
                start_index = self.yamnet_samples_index
                end_index = self.yamnet_samples_index + len_to_extract
                if len_to_extract < len(waveform):
                    waveform = waveform[:len_to_extract]
                self.yamnet_samples[start_index:end_index] = waveform
                self.yamnet_samples_index += len_to_extract
                processing_time += time.time() - deb
                if self.yamnet_samples_index < len(self.yamnet_samples):
                    # window is not complete so wait for more samples
                    continue
                self.yamnet_samples_index = 0 # reset index
                sum_samples = np.sum(
                    np.power(waveform / reference_pressure, 2.0))
                leq = 10 * math.log10(sum_samples / len(waveform))
                if leq >= self.config.min_leq:
                    print("Leq: %.2f dB > %.2f dB, so now try to recognize"
                          " sound source " % (leq, self.config.min_leq))
                    deb = time.time()
                    scores, embeddings, spectrogram = self.process_tags()
                    processing_time += time.time() - deb
                    # Take maximum found prediction (was avg in the ref)
                    prediction = np.max(scores, axis=0)
                    # filter out classes that are below threshold values
                    filter_pred = (prediction > self.yamnet_classes[1])
                    filter_pred = filter_pred.nonzero()[0]
                    classes_threshold_index = list(map(int, filter_pred))
                    # If trigger_tag defined, we process only if one of the
                    # tag is specified
                    keep_classification = len(self.config.trigger_tag) == 0
                    if len(self.config.trigger_tag) > 0:
                        classification_tag = [self.yamnet_classes[0][i]
                                              for i in classes_threshold_index]
                        for tag in self.config.trigger_tag:
                            if tag in classification_tag:
                                print("Do not record audio because %s has"
                                      " been detected" % banned_tag)
                                keep_classification = True
                                break
                    if len(classes_threshold_index) == 0 or\
                            not keep_classification:
                        # classifier rejected all known classes
                        print("No classes found above yamnet threshold")
                        self.samples_stack.clear()
                        status = "wait_trigger"
                        continue
                    # Sort by score
                    classes_threshold_index = [classes_threshold_index[j] for j in
                               np.argsort([prediction[i]-self.yamnet_classes[1][i]
                                           for i in classes_threshold_index])[::-1]]
                    # Compute a score between 0-100% from threshold to 1.0
                    scores_percentage = {self.yamnet_classes[0][i]: round(
                        float(((prediction[i] - self.yamnet_classes[1][i]) / (
                                    1 - self.yamnet_classes[1][i])) * 100)) for
                                         i in classes_threshold_index}
                    document_scores = {self.yamnet_classes[0][i]:
                              round(prediction[i], 2) for i in
                              classes_threshold_index}
                    # threshold_time is the score over the time
                    # there is 2x more cells because there is 50% overlap
                    threshold_time = {
                        self.yamnet_classes[0][i]: [self.yamnet_classes[1][i],
                                                    np.round(scores[:, i],
                                                             3).tolist()]
                        for i in classes_threshold_index}
                    document = {"scores": document_scores,
                                "scores_perc": scores_percentage,
                                "scores_time": threshold_time,
                                "leq": round(leq, 2),
                                "epoch_millisecond": int(cur_time)}
                    tags = ' '.join('{:s}({:d}%)'.format(k, v)
                                    for k, v in scores_percentage.items())
                    print("%s tags:%s \n processed in %.3f seconds for "
                          "%.1f seconds of audio." %
                          (time.strftime("%Y-%m-%d %H:%M:%S"),
                           tags, processing_time,
                           len(self.yamnet_samples) /
                           self.yamnet_config.sample_rate))
                    processing_time = 0
                    if self.remaining_triggers >= 0:
                        print(" Remaining triggers for today %d" %
                              self.remaining_triggers)
                    # check for audio storage tag exception
                    banned = False
                    if len(self.config.trigger_ban) > 0:
                        for banned_tag in self.config.trigger_ban:
                            if banned_tag in document["scores"].keys():
                                print("Do not record audio because %s has"
                                      " been detected" % banned_tag)
                                banned = True
                                break
                    if not banned and self.config.total_length > 0 and (
                            self.remaining_triggers > 0 or
                            self.remaining_triggers == -1):
                        # requesting audio data into the json file, so now record audio
                        status = "record"
                        continue
                    else:
                        # no audio record, just send the recognized tags
                        status = "wait_trigger"
                        self.socket_out.send_json(document)
                        continue
                processing_time = 0 # yamnet window has been rejected
            elif status == "record":
                if self.remaining_triggers > 0:
                    self.remaining_triggers -= 1
                while status == "record":
                    audio_data_encrypt = ""
                    ssh_file = os.path.expanduser(self.config.ssh_file)
                    if "AES" in globals() and os.path.exists(ssh_file):
                        # empty audio cache
                        samples_trigger = io.BytesIO()
                        remaining_samples = int(self.config.total_length * self.config.sample_rate)
                        while len(self.samples_stack) > 0:
                            audio_samples = self.samples_stack.popleft()
                            remaining_samples -= len(audio_samples)
                            samples_trigger.write(audio_samples.tobytes())
                        # read audio samples until remaining_samples reached
                        while remaining_samples > 0:
                            audio_samples = self.fetch_audio_data(False)
                            remaining_samples -= len(audio_samples)
                            samples_trigger.write(audio_samples.tobytes())
                        audio_processing_start = time.time()
                        # Compress audio samples
                        output = io.BytesIO()
                        data, samplerate = sf.read(samples_trigger, format='RAW',
                                                   channels=1,
                                                   samplerate=int(self.config.sample_rate),
                                                   subtype='PCM_32')
                        channels = 1
                        with sf.SoundFile(output, 'w', samplerate, channels, format='OGG') as f:
                            f.write(data)
                            f.flush()
                        audio_data_encrypt = base64.b64encode(encrypt(output.getvalue(), ssh_file)).decode("UTF-8")
                        print("raw %d array %d bytes b64 ogg: %d bytes in %.3f seconds" % (
                            samples_trigger.tell(), data.shape[0], len(audio_data_encrypt),
                            time.time() - audio_processing_start))
                        info = sf.info(io.BytesIO(output.getvalue()))
                        print("Audio duration %.2f s, remaining triggers %d" % (info.duration, self.remaining_triggers))
                    document["encrypted_audio"] = audio_data_encrypt
                    self.socket_out.send_json(document)
                    self.samples_stack.clear()
                    status = "wait_trigger"
            time.sleep(0.05)

    def unix_time(self):
        return (datetime.datetime.utcnow() - self.epoch).total_seconds()


if __name__ == "__main__":
    required_actions = []
    parser = argparse.ArgumentParser(description='This program read audio stream from zeromq and publish noise events',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--trigger_count", help="limit the number of embedding of audio by day (-1 unlimited)", default=-1, type=int)
    parser.add_argument("-b", "--trigger_ban", help="Remove storage of audio if one of the following audio recognition tag is detected",
                        action='append', type=str)
    parser.add_argument("-c", "--configuration_file", help="Provide json configuration file instead of arguments", default="", type=str)
    parser.add_argument("-t", "--trigger_tag", help="Send json only if this tags are detected",
                        action='append', type=str, default=[])
    parser.add_argument("--min_leq", help="minimum leq to trigger an event", default=30, type=float)
    parser.add_argument("--total_length", help="record length total in seconds to be embedded into the output json", default=10, type=float)
    parser.add_argument("--cached_length", help="record length before the trigger", default=5, type=float)
    parser.add_argument("--sample_rate", help="audio sample rate", default=48000, type=int)
    parser.add_argument("--resample_method", help="Resampling method as Yamnet is requiring 16 KHz", default='kaiser_fast', type=str)
    parser.add_argument("--sample_format", help="audio format", default="FLOAT_LE")
    parser.add_argument("--ssh_file", help="public key file for audio encryption", default="~/.ssh/id_rsa.pub")
    parser.add_argument("--input_address", help="Address for zero_record samples", default="tcp://127.0.0.1:10001")
    parser.add_argument("--output_address", help="Address for publishing JSON of sound recognition",
                        default="tcp://*:10002")
    required_actions.append(parser.add_argument("--yamnet_class_map", help="Yamnet CSV path yamnet_class_threshold_map.csv", type=str))
    required_actions.append(parser.add_argument("--yamnet_weights", help="Yamnet .tflite model download at https://tfhub.dev/google/lite-model/yamnet/tflite/1", type=str))
    parser.add_argument("--yamnet_cutoff_frequency", help="Yamnet highpass filter frequency", default=0, type=float)
    parser.add_argument("--yamnet_max_gain", help="Yamnet maximum gain in dB", default=8.0, type=float)
    parser.add_argument("--yamnet_window_time", help="Sound source recognition time in seconds", default=5.0,
                        type=int)
    parser.add_argument("--sensitivity", help="Microphone sensitivity in dBFS at 94 dB 1 kHz", default=-28.34,
                        type=float)
    parser.add_argument("--delay_print_samples", help="Delay in second between each print of number of samples read",
                        default=0, type=float)
    args = parser.parse_args()
    if not args.configuration_file:
        # no configuration file but configured with command line arguments
        # Set-up mandatory arguments
        for action in required_actions:
            action.required = True
        args = parser.parse_args()
    else:
        with open(args.configuration_file, "r") as fp:
            print("Load configuration file " + args.configuration_file)
            cfg = json.load(fp)
            args = types.SimpleNamespace(**cfg)
    print("Configuration:\n" + json.dumps(vars(args),
          sort_keys=False, indent=2))
    trigger = TriggerProcessor(args)
    args.running = True
    status_thread = StatusThread(trigger, args)
    if args.delay_print_samples > 0:
        # run stats thread
        status_thread.start()
    try:
        trigger.run()
    finally:
        args.running = False
