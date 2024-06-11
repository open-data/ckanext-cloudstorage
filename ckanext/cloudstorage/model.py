#!/usr/bin/env python
# -*- coding: utf-8 -*-
from datetime import datetime

import ckan.model.meta as meta
from ckan.model.domain_object import DomainObject
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    UnicodeText,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref, relationship

Base = declarative_base()
metadata = Base.metadata


class MultipartPart(Base, DomainObject):
    __tablename__ = "cloudstorage_multipart_part"

    def __init__(self, n, etag, upload):
        self.n = n
        self.etag = etag
        self.upload = upload

    n = Column(Integer, primary_key=True)
    etag = Column(UnicodeText, primary_key=True)
    upload_id = Column(
        UnicodeText,
        ForeignKey("cloudstorage_multipart_upload.id"),
        primary_key=True,
    )
    upload = relationship(
        "MultipartUpload",
        backref=backref("parts", cascade="delete, delete-orphan"),
        single_parent=True,
    )


class MultipartUpload(Base, DomainObject):
    __tablename__ = "cloudstorage_multipart_upload"

    def __init__(self, id, resource_id, name, size, original_name, user_id):
        self.id = id
        self.resource_id = resource_id
        self.name = name
        self.size = size
        self.original_name = original_name
        self.user_id = user_id

    @classmethod
    def resource_uploads(cls, resource_id):
        query = meta.Session.query(cls).filter_by(resource_id=resource_id)
        return query

    id = Column(UnicodeText, primary_key=True)
    resource_id = Column(UnicodeText)
    name = Column(UnicodeText)
    initiated = Column(DateTime, default=datetime.utcnow)
    size = Column(Numeric)
    original_name = Column(UnicodeText)
    user_id = Column(UnicodeText)
