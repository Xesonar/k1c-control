import unittest
from video_request import clip_seconds


class ClipRequestTests(unittest.TestCase):
    def test_expected_requests(self):
        for text, seconds in [('30 сек',30),('15 секунд',15),('видео',30),('/video',30),
                              ('/video@mybot 60',60),('клип 2 мин',120),('последние 30 секунд печати видео',30),
                              ('5 мин',300),('видео 1,5 мин',90)]:
            with self.subTest(text=text):self.assertEqual(clip_seconds(text),seconds)

    def test_not_a_video_request(self):
        for text in ['сопло 240','сколько осталось','покажи камеру','/photo','прогресс','30','температура 30']:
            with self.subTest(text=text):self.assertIsNone(clip_seconds(text))

    def test_invalid_explicit_duration(self):
        for text in ['видео 301','0 сек','видео -1','/video 20 часов','видео 1.2 сек']:
            with self.subTest(text=text):self.assertRaises(ValueError,clip_seconds,text)
